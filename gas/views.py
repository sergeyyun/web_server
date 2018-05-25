# views.py
#
# Copyright (C) 2011-2018 Vas Vasiliadis
# University of Chicago
#
# Application logic for the GAS
#
##
__author__ = 'Vas Vasiliadis <vas@uchicago.edu>'

import uuid
import time
import json
from datetime import datetime

import boto3
from botocore.client import Config
from boto3.dynamodb.conditions import Key

from flask import (abort, flash, redirect, render_template,
  request, session, url_for, jsonify)

from gas import app, db
from decorators import authenticated, is_premium
from auth import get_profile, update_profile

import re

"""Start annotation request
Create the required AWS S3 policy document and render a form for
uploading an annotation input file using the policy document
"""
@app.route('/annotate', methods=['GET'])
@authenticated
def annotate():
  # Open a connection to the S3 service
  s3 = boto3.client('s3',
    region_name=app.config['AWS_REGION_NAME'],
    config=Config(signature_version='s3v4'))

  bucket_name = app.config['AWS_S3_INPUTS_BUCKET']
  user_id = session['primary_identity']

  # Generate unique ID to be used as S3 key (name)
  key_name = app.config['AWS_S3_KEY_PREFIX'] + user_id + '/' + str(uuid.uuid4()) + '~${filename}'

  # Redirect to a route that will call the annotator
  redirect_url = str(request.url) + "/job"

  # Define policy conditions
  # NOTE: We also must inlcude "x-amz-security-token" since we're
  # using temporary credentials via instance roles
  encryption = app.config['AWS_S3_ENCRYPTION']
  acl = app.config['AWS_S3_ACL']
  expires_in = app.config['AWS_SIGNED_REQUEST_EXPIRATION']
  fields = {
    "success_action_redirect": redirect_url,
    "x-amz-server-side-encryption": encryption,
    "acl": acl
  }
  conditions = [
    ["starts-with", "$success_action_redirect", redirect_url],
    {"x-amz-server-side-encryption": encryption},
    {"acl": acl}
  ]

  # Generate the presigned POST call
  presigned_post = s3.generate_presigned_post(Bucket=bucket_name,
    Key=key_name, Fields=fields, Conditions=conditions, ExpiresIn=expires_in)

  # Render the upload form which will parse/submit the presigned POST
  return render_template('annotate.html', s3_post=presigned_post)


"""Fires off an annotation job
Accepts the S3 redirect GET request, parses it to extract
required info, saves a job item to the database, and then
publishes a notification for the annotator service.
"""
@app.route('/annotate/job', methods=['GET'])
@authenticated
def create_annotation_job_request():
  # Parse redirect URL query parameters for S3 object info
  bucket_name = request.args.get('bucket')
  key_name = request.args.get('key')

  # Extract the job ID from the S3 key
  #extract uuid from key using regular expressions
  job_id = ""
  m = re.search('/.+/(.+?)~', key_name)
  try:
    job_id = m.group(1)
  except Exception:
    response = 'Error: could not extract job ID from file key'
    return jsonify({'code': 500, 'message': response})

#extract file name from key using regular expressions
  input_file_name = ""
  m = re.search('~(.*)', key_name)
  try:
    input_file_name = m.group(1)
  except Exception:
    response = 'Error: could not extract file name from file key'
    return jsonify({'code': 500, 'message': response})

  # Persist job to database
  #create a job item

  user_id = session['primary_identity']
  user_profile = get_profile(identity_id=session['primary_identity'])
  user_email = user_profile.email
  user_role = user_profile.role

  data = {
      'job_id': job_id,
      'user_id': user_id,
      'user_email': user_email,
      'user_role': user_role,
      'input_file_name': input_file_name,
      's3_inputs_bucket': bucket_name,
      's3_key_input_file': key_name,
      'submit_time': int(time.time()),
      'job_status': 'PENDING'
  }

  #upload job item to the database
  dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

  ann_table_name = app.config['AWS_DYNAMODB_ANNOTATIONS_TABLE']
  ann_table = dynamodb.Table(ann_table_name)
  try:
    ann_table.put_item(Item = data)
  except Exception:
    response = 'Error: could not save job in the database'
    return jsonify({'code': 500, 'message': response})


  # Send message to request queue
  sns = boto3.client('sns', region_name='us-east-1')
  try:
      sns.publish(
          TopicArn = app.config['AWS_SNS_JOB_REQUEST_TOPIC'],
          Message = json.dumps(data)
      )
  except Exception:
      response = 'Error: could not publish message to SNS'
      return jsonify({'code': 500, 'message': response})

  return render_template('annotate_confirm.html', job_id=job_id)


"""List all annotations for the user
"""
@app.route('/annotations', methods=['GET'])
@authenticated
def annotations_list():
  user_id = session['primary_identity']

  #connect to the dynamodb database
  try:
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    ann_table = dynamodb.Table(app.config['AWS_DYNAMODB_ANNOTATIONS_TABLE'])
  except Exception:
    print("Error: failed to connect to the database")

  # Get list of annotations to display
  job_list = []
  response = ann_table.query(IndexName='user_id_index', KeyConditionExpression=Key('user_id').eq(user_id))
  data = response.get('Items')

  #parse the data into a list of dicts
  for item in data:
    job_details = {'job_id':None, 'request_time':None, 'file_name':None, 'status':None}
    job_details['job_id'] = item['job_id']
    job_details['request_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(item['submit_time'])) #https://stackoverflow.com/questions/12400256/converting-epoch-time-into-the-datetime
    job_details['file_name'] = item['input_file_name']
    job_details['status'] = item['job_status']
    job_list.append(job_details)

  #render annotations template and pass the job list to it
  return render_template('annotations.html', annotations=job_list)


"""Display details of a specific annotation job
"""
@app.route('/annotations/<id>', methods=['GET'])
@authenticated
def annotation_details(id):

  #connect to the dynamodb database
  try:
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    ann_table = dynamodb.Table(app.config['AWS_DYNAMODB_ANNOTATIONS_TABLE'])
  except Exception:
    print("Error: failed to connect to the database")

  #check if the job belongs to current user
  user_id = session['primary_identity']
  response = ann_table.query(IndexName='user_id_index', KeyConditionExpression=Key('user_id').eq(user_id))
  data = response.get('Items')
  job_list = []
  for item in data:
    job_id = item['job_id']
    job_list.append(job_id)

  if id in job_list:
    # Get annotation to display
    response = ann_table.get_item(Key={'job_id': id})
    data = response.get('Item')

    job_details = {'job_id':None, 'request_time':None, 'file_name':None, 'status':None, 'complete_time':None, 'results_file':None, 'log_file':None}

    #check if the job is complete
    if data['job_status'] == "COMPLETED":
      #generate presigned download URL for results file_name
      s3 = boto3.client('s3')
      url = s3.generate_presigned_url(
        ClientMethod='get_object',
        Params={
          'Bucket':app.config['AWS_S3_RESULTS_BUCKET'],
          'Key':data['s3_key_result_file']
        }
      )
      job_details['results_file'] = url

      #generate a url for the job log contents page
      log_file = data['job_id'] + '/log'
      job_details['log_file'] = log_file

      #record completion time
      job_details['complete_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(data['complete_time']))

    #parse the data into a dict
    job_details['job_id'] = data['job_id']
    job_details['request_time'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(data['submit_time'])) #https://stackoverflow.com/questions/12400256/converting-epoch-time-into-the-datetime
    job_details['file_name'] = data['input_file_name']
    job_details['status'] = data['job_status']

    return render_template('job_details.html', job_details=job_details)

  else:
    return render_template('job_details.html', job_details="not_authorized")


"""Display the log file for an annotation job
"""
@app.route('/annotations/<id>/log', methods=['GET'])
@authenticated
def annotation_log(id):
  #connect to the dynamodb database
  try:
    dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
    ann_table = dynamodb.Table(app.config['AWS_DYNAMODB_ANNOTATIONS_TABLE'])
  except Exception:
    print("Error: failed to connect to the database")

  #check if the job belongs to current user
  user_id = session['primary_identity']
  response = ann_table.query(IndexName='user_id_index', KeyConditionExpression=Key('user_id').eq(user_id))
  data = response.get('Items')
  job_list = []
  for item in data:
    job_id = item['job_id']
    job_list.append(job_id)

  if id in job_list:
    # Get log file
    response = ann_table.get_item(Key={'job_id': id})
    data = response.get('Item')
    s3_key_log_file = data['s3_key_log_file']

    #read log file contents
    s3 = boto3.resource('s3')
    log_file = s3.Object(app.config['AWS_S3_RESULTS_BUCKET'], s3_key_log_file)
    log = log_file.get()['Body'].read().decode('utf-8')

    return render_template('job_log.html', log=log)

  else:
    return render_template('job_log.html', log="not_authorized")



"""Subscription management handler
"""
import stripe

@app.route('/subscribe', methods=['GET', 'POST'])
@authenticated
def subscribe():

#https://stripe.com/docs/

  if request.method == 'POST':
    stripe.api_key = app.config['STRIPE_SECRET_KEY']
    token = request.form['stripe_token']

    # Create a Customer:
    user_id = session['primary_identity']
    user_profile = get_profile(identity_id=session['primary_identity'])
    user_email = user_profile.email
    customer = stripe.Customer.create(
      source=token,
      email=user_email,
    )

    # Create the Subscription
    stripe.Subscription.create(
      customer=customer.id,
      items=[
        {
          "plan": "premium_plan",
          "quantity": 1,
        },
      ]
    )

    #update profile in the database
    update_profile(
      identity_id=session['primary_identity'],
      role="premium_user")

    customer_id = customer.id
    print("successfully charged customer ID " + customer_id)
    return render_template('subscribe_confirm.html', stripe_id=customer_id)

  else:
    return render_template('subscribe.html')





"""DO NOT CHANGE CODE BELOW THIS LINE
*******************************************************************************
"""

"""Home page
"""
@app.route('/', methods=['GET'])
def home():
  return render_template('home.html')

"""Login page; send user to Globus Auth
"""
@app.route('/login', methods=['GET'])
def login():
  app.logger.info('Login attempted from IP {0}'.format(request.remote_addr))
  # If user requested a specific page, save it to session for redirect after authentication
  if (request.args.get('next')):
    session['next'] = request.args.get('next')
  return redirect(url_for('authcallback'))

"""404 error handler
"""
@app.errorhandler(404)
def page_not_found(e):
  return render_template('error.html',
    title='Page not found', alert_level='warning',
    message="The page you tried to reach does not exist. Please check the URL and try again."), 404

"""403 error handler
"""
@app.errorhandler(403)
def forbidden(e):
  return render_template('error.html',
    title='Not authorized', alert_level='danger',
    message="You are not authorized to access this page. If you think you deserve to be granted access, please contact the supreme leader of the mutating genome revolutionary party."), 403

"""405 error handler
"""
@app.errorhandler(405)
def not_allowed(e):
  return render_template('error.html',
    title='Not allowed', alert_level='warning',
    message="You attempted an operation that's not allowed; get your act together, hacker!"), 405

"""500 error handler
"""
@app.errorhandler(500)
def internal_error(error):
  return render_template('error.html',
    title='Server error', alert_level='danger',
    message="The server encountered an error and could not process your request."), 500

### EOF
