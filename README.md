qbo-backend (production)
------------------------

Files:
- app_prod.py  (Flask production backend)
- requirements.txt
- migrate.sql
- migrate.py
- Procfile

Set environment variables before deploy:
- QBO_CLIENT_ID
- QBO_CLIENT_SECRET
- QBO_REALM_ID
- QBO_REDIRECT_URI  (https://<your-backend>.onrender.com/callback)
- FRONTEND_URL      (https://<your-frontend>.vercel.app)
- RECEIPTS_API_KEY  (optional)
- DATABASE_URL      (Postgres connection string)

Deploy to Render:
- Build: pip install -r requirements.txt
- Start: gunicorn -w 4 -b 0.0.0.0:$PORT app_prod:app
