"""
Enhanced Flask app with:
- Render Postgres SSL fixes
- Clean SalesReceipt parsing
- Auto-generate thermal receipt text
- Pagination + DB caching for receipts
- /receipt/<id> lookup API
- PDF generation (ReportLab)
- Webhook endpoint to cache new receipts
- Background worker to refresh QBO tokens automatically


Dependencies (add to requirements.txt):
Flask
Flask-Cors
SQLAlchemy
psycopg2-binary
requests
cachetools
reportlab


Place this file as app.py; keep your frontend in ../qbo-frontend/dist as before.
"""


import os
import time
import json
import threading
import queue
from io import BytesIO
from datetime import datetime, timedelta


import requests
from flask import Flask, redirect, request, jsonify, send_from_directory, make_response, abort
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, JSON, func
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import SQLAlchemyError
from cachetools import TTLCache
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


# --------------------------- Configuration ---------------------------
CLIENT_ID = os.environ.get('QBO_CLIENT_ID')
CLIENT_SECRET = os.environ.get('QBO_CLIENT_SECRET')
REALM_ID = os.environ.get('QBO_REALM_ID')
REDIRECT_URI = os.environ.get('QBO_REDIRECT_URI')
FRONTEND_URL = os.environ.get('FRONTEND_URL')
RECEIPTS_API_KEY = os.environ.get('RECEIPTS_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
TOKEN_FILE = os.environ.get('TOKEN_FILE', 'tokens.json')


# App
app = Flask(__name__, static_folder='../qbo-frontend/dist', static_url_path='/')
if FRONTEND_URL:
CORS(app, resources={r"/receipts": {"origins": FRONTEND_URL}, r"/connect": {"origins": FRONTEND_URL}, r"/callback": {"origins": FRONTEND_URL}})
else:
CORS(app)


# --------------------------- Database / Models ---------------------------
Base = declarative_base()
engine = None
SessionLocal = None


class Token(Base):
__tablename__ = 'tokens'
id = Column(Integer, primary_key=True, index=True)
access_token = Column(Text, nullable=True)
refresh_token = Column(Text, nullable=True)
token_type = Column(String(64), nullable=True)
expires_at = Column(Float, nullable=True)
created_at = Column(Float, default=time.time)
