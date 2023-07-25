from flask import Flask
from flask import request, make_response
from flask_cors import CORS
from FishHunterUtil.logger import init_logging
from dotenv import load_dotenv
import logging
from uuid import uuid4
from datetime import datetime
from WebPageClone import save_html
from FishHunterUtil.features_extractor import get_dataset_features
from FishHunterUtil.comparer import Comparer

import os
from pymongo import MongoClient
from bson import ObjectId
import json
import tldextract
import threading

max_threads = 9999
thread_semaphore = threading.BoundedSemaphore(max_threads)
threads = []
list_result = []
app_port=5555

app = Flask(__name__)

config = {
    "DEBUG": True  # run app in debug mode
}
app.config.from_mapping(config)

CORS(app, resource={
    r"/*":{
        "origins":"*"
    }
})

init_logging()
logger = logging.getLogger(__name__)
load_dotenv()

CSS_WEIGHT=float(os.getenv("CSS_WEIGHT"))
HTML_WEIGHT=float(os.getenv("HTML_WEIGHT"))
TEXT_WEIGHT=float(os.getenv("TEXT_WEIGHT"))

MONGO_CLIENT = MongoClient(os.getenv("MONGO_CONNECTION_STRING"))
DB = MONGO_CLIENT['hunter']
DB_SAMPLES = MONGO_CLIENT['hunter_samples']

SAMPLES = DB["samples"]
ALLOW_LIST = DB["allow_list"]
BRANDS = DB["brands"]

def calculate(ds, sample, feature):
    global threads, list_result
    try:
        thread_semaphore.acquire()

        # get dataset features
        feat_ds = ds["features"]
        f_text = feat_ds["text"]
        f_html = json.loads(feat_ds["html"])
        f_css = json.loads(feat_ds["css"])

        # get sample features
        feat_sm = sample["features"]
        sample_text = feat_sm["text"]
        sample_html = json.loads(feat_sm["html"])
        sample_css = json.loads(feat_sm["css"])

        score = 0
        if feature == "css":
            comparer = Comparer()
            score, time_seconds = comparer.compare_by_css(f_css, sample_css)
        elif feature == "html":
            comparer = Comparer()
            score, time_seconds = comparer.compare_by_html(f_html, sample_html)
        elif feature == "text":
            comparer = Comparer()
            score, time_seconds = comparer.compare_by_text(f_text, sample_text)
        else:
            comparer = Comparer()
            score_css, time_seconds_css = comparer.compare_by_css(f_css, sample_css)
            score_html, time_seconds_html = comparer.compare_by_html(f_html, sample_html)
            score_text, time_seconds_text = comparer.compare_by_text(f_text, sample_text)
            score = score_css*CSS_WEIGHT + score_html*HTML_WEIGHT + score_text*TEXT_WEIGHT

        list_result.append({
            "brands": sample["brands"],
            "ref_sample": str(sample["_id"]),
            "url": sample["url"],
            "score": score
        })

    except Exception as e:
        print(e)
    finally:
        thread_semaphore.release()

def calculate_similarity(ds, feature):
    global list_result, threads
    threads = []
    list_result = []

    allow_list_samples = list(SAMPLES.find({
        "type": "allow_list_generated" # DISABLE THIS FOR ALL SAMPLES
    }))

    sample_collection_name = os.getenv(feature.upper() + "_SAMPLE_COLLECTION")
    generated_sample_col = DB_SAMPLES[sample_collection_name]
    generated_sample_list = list(generated_sample_col.find())

    # combine allow list and generated list
    list_samples = allow_list_samples + generated_sample_list

    for sample in list_samples:
        t = threading.Thread(target=calculate, args=(ds, sample, feature))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return list_result

@app.before_request
def before_request():
    logger.info("Request received")

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', '*')
    response.headers.add('Access-Control-Allow-Methods', '*')
    return response    

@app.route('/')
def index():
    return '...'

@app.route('/scan', methods=['POST'])
def scan():
    logger.info("Scanning website")
    data = request.get_json()

    logger.info("Data: " + str(data))

    url = data["url"]
    html_dom = data["html_dom"]
    feature = data["feature"]

    domain = tldextract.extract(url).domain
    apex_domain = tldextract.extract(url).registered_domain

    # check if domain is in allow list
    if ALLOW_LIST.find_one({"domain": domain}) is not None or ALLOW_LIST.find_one({"domain": apex_domain}) is not None:
        return json.dumps({
            "status": "success",
            "data": {
                "eventid": None,
                "similarity": 0
            }
        })

    eventid = datetime.now().strftime('%Y%m-%d%H-%M%S-') + str(uuid4())
    temp_path = f"files/{eventid}/"

    # create temp folder
    os.makedirs(temp_path)
    save_html(url, html_content=html_dom, saved_path=temp_path)

    f_text, f_html, f_css = get_dataset_features(temp_path)

    logger.info("Feature extraction completed")
    logger.info("Text:" + f_text)
    logger.info("HTML:" + json.dumps(f_html))
    logger.info("CSS:" + json.dumps(f_css))

    # create temporary dataset
    ds = {
        "features": {
            "text": f_text,
            "html": f_html,
            "css": f_css
        }
    }

    # calculate similarity
    start_time = datetime.now()
    similarity_res = calculate_similarity(ds, feature)
    end_time = datetime.now()
    total_time_seconds = (end_time - start_time).total_seconds()

    logger.info("Done calculating similarity")
    logger.info("Total time: {} sec", total_time_seconds)

    # sort by score
    similarity_res = sorted(similarity_res, key=lambda k: k['score'], reverse=True)
    print(similarity_res[0])
    best_score = similarity_res[0]["score"]

    if len(similarity_res[0]["brands"]) == 0:
        brand = "-"
    else:
        brand = similarity_res[0]["brands"][0]

    # Get brand info
    brand_info = BRANDS.find_one({"identifier": brand})
    if brand_info is None:
        brand_info = {}

    # add application/json header
    response = make_response(json.dumps({
        "status": "success",
        "data": {
            "eventid": eventid,
            "similarity": best_score,
            "brand": brand_info.get("name", ""),
        }
    }))
    response.headers['Content-Type'] = 'application/json'

    return response

if __name__ == '__main__':
    app.run(port=app_port)
