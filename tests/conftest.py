import os

# Set required env before server module loads
os.environ.setdefault("PG_USER", "test_user")
os.environ.setdefault("PG_PASSWORD", "test_password")
os.environ.setdefault("CURATED_QUERIES_FILE", "/nonexistent/path/curated-queries.yaml")
