#/usr/bin/env Python3

import sys
import os
import re
import pandas as pd
import s3fs
import boto3
import botocore
import random, json
from datetime import datetime, timedelta
from fastparquet import ParquetFile
from sklearn import preprocessing
from flask import Flask, render_template, request, redirect, Response, url_for, jsonify
from flask_jsglue import JSGlue

app = Flask(__name__)
JSGlue(app)

@app.route('/')
def output():
    # load existing sensor network
    try:
        # load from local instance
        print("Looking for sensor file locally.", flush=True)
        df = pd.read_parquet("./pasensors.parquet")
    except:
        # otherwise go to S3. Try today's date first, then iteratively look backward day by day
        print("No local sensor file. Searching S3.", flush=True)
        file_date = datetime.today()
        while True:
            try:
                filename = file_date.strftime('%Y%m%d') + ".parquet"
                print("Looking for file " + filename, flush=True)
                s3 = s3fs.S3FileSystem()
                myopen = s3.open
                s3_resource = boto3.resource('s3')
                s3_resource.Object('midscapstone-whos-polluting-my-air', 'PurpleAirDaily/{}'.format(filename)).load()
                pf = ParquetFile('midscapstone-whos-polluting-my-air/PurpleAirDaily/{}'.format(filename), open_with=myopen)
                df = pf.to_pandas()
                break
            except botocore.exceptions.ClientError:
                file_date = file_date - timedelta(days=1)
    global unique_sensor_df
    unique_sensor_df = df.drop_duplicates(subset="sensor_id")

    # load polluters
    global polluter_df
    try:
        print("Looking for polluter file locally.", flush=True)
        polluter_df = pd.read_csv("./polluters.csv")
    except:
        print("No local polluter file. Searching S3.", flush=True)
        bucket = "midscapstone-whos-polluting-my-air"
        s3 = boto3.client('s3')
        obj = s3.get_object(Bucket=bucket, Key='UtilFiles/polluters.csv')
        polluter_df = pd.read_csv(obj['Body'])

    # Load predictions
    global df_predictions
    try:
        print("Looking for prediction file locally.", flush=True)
        df_predictions = pd.read_csv("./preds_loneliness.csv")
    except:
        print("No local prediction file. Searching S3.", flush=True)
        bucket = "midscapstone-whos-polluting-my-air"
        s3 = boto3.client('s3')
        # obj = s3.get_object(Bucket= bucket, Key= 'UtilFiles/preds.csv')
        obj = s3.get_object(Bucket=bucket, Key='UtilFiles/preds_loneliness.csv')
        df_predictions = pd.read_csv(obj['Body'])
    df_predictions.drop(['xy_'], axis=1, inplace=True)
    # df_predictions[['lat', 'lon']] = df[['lat', 'lon']].apply(pd.to_numeric)

    # normalize predictions
    min_max_scaler = preprocessing.MinMaxScaler()
    preds = df_predictions[['preds']].values.astype(float)
    preds_normalized = min_max_scaler.fit_transform(preds)
    df_predictions['preds_normalized'] = preds_normalized

    # normalize loneliness
    lonely = df_predictions[['lonely_factor']].values.astype(float)
    lonely_factor_normalized = min_max_scaler.fit_transform(lonely)
    df_predictions['lonely_factor_normalized'] = lonely_factor_normalized

    # create combined score
    loneliness_weight = 1
    df_predictions['score'] = loneliness_weight * df_predictions['preds_normalized'] + \
                              (1 - loneliness_weight) * df_predictions['lonely_factor_normalized']
    print(df_predictions.head(), flush=True)

    # serve index template
    return render_template('index.html')

@app.route("/update")
def update():
    """Find lat lon for desired number of sensors in the bounding box."""

    # ensure parameters are present
    if not request.args.get("sw"):
        raise RuntimeError("missing sw")
    if not request.args.get("ne"):
        raise RuntimeError("missing ne")


    # ensure parameters are in lat,lng format
    if not re.search("^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$", request.args.get("sw")):
        raise RuntimeError("invalid sw")
    if not re.search("^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$", request.args.get("ne")):
        raise RuntimeError("invalid ne")

    # Get desired number of sensors
    if not request.args.get("q"):
        q=0
    else:
        q = int(request.args.get("q"))

    # Check if we need to display existing sensors
    toggle_existing = request.args.get("toggle_existing")
    if toggle_existing == 'false':
        toggle_existing = False
    elif toggle_existing == 'true':
        toggle_existing = True
    else:
        print("Error in toggle_existing", toggle_existing)

    # Check if we need to display polluters
    toggle_polluters = request.args.get("toggle_polluters")
    if toggle_polluters == 'false':
        toggle_polluters = False
    elif toggle_polluters == 'true':
        toggle_polluters = True
    else:
        print("Error in toggle_polluters", toggle_polluters)

    if toggle_existing:
        existing_lat = unique_sensor_df.lat.tolist()
        existing_lon = unique_sensor_df.lon.tolist()
        existing_name = unique_sensor_df.sensor_name.tolist()
        existing_lst = list(zip(existing_lat, existing_lon, existing_name))
    else:
        existing_lst = []

    if toggle_polluters:
        polluter_lat = polluter_df.Lat.tolist()
        polluter_lon = polluter_df.Lon.tolist()
        polluter_name = polluter_df.Name.tolist()
        polluter_street = polluter_df.Street.tolist()
        polluter_city = polluter_df.City.tolist()
        polluter_pm = polluter_df.PM.tolist()
        polluter_lst = list(zip(polluter_lat, polluter_lon, polluter_name, polluter_street, polluter_city, polluter_pm))
    else:
        polluter_lst = []

    # explode southwest corner into two variables
    (sw_lat, sw_lng) = [float(s) for s in request.args.get("sw").split(",")]

    # explode northeast corner into two variables
    (ne_lat, ne_lng) = [float(s) for s in request.args.get("ne").split(",")]

    # load predictions and select recommendations
    loc_lst = []
    # try:
    # filter by current bounding box
    df_filtered = df_predictions[(df_predictions.lat > sw_lat) & (df_predictions.lat < ne_lat) &
                                 (df_predictions.lon > sw_lng) & (df_predictions.lon < ne_lng)]
    df_filtered.reset_index(inplace=True, drop=True)
    print(df_filtered.head(), flush=True)

    # sort and select top candidates
    df_sorted = df_filtered.sort_values(by='score', ascending=False)
    top_lat = df_sorted.head(q).lat.tolist()
    top_lon = df_sorted.head(q).lon.tolist()
    loc_lst = list(zip(top_lat, top_lon))
    #     print("*** LOCATION ***: {}".format(loc_lst))
    # except Exception as e:
    #     print("*** EXCEPTION IN GET ADDRESS: {}".format(e), flush=True)

    location_json = {
        "recommendations": loc_lst,
        "existing": existing_lst,
        "polluters": polluter_lst
    }
    return jsonify(location_json)

if __name__ == '__main__':
    app.run("0.0.0.0", "8083")
