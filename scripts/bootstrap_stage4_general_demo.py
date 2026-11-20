"""Offline schema preparation only for the named isolated Stage4 demo DB."""
from app.intelligence2.stage4_general_homologation import bootstrap_schema

if __name__ == "__main__":
    bootstrap_schema()
