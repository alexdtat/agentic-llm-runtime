import os

def configuration():
    return os.environ.get("DB_URL")
