"""
Setup Philosophy RAG Corpus
"""

import sys
# Windows console (CP1250) can't print emojis - force UTF-8 with replacement
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass


import os
import sys
# from google.cloud import aiplatform
import vertexai
from vertexai.preview import rag
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def setup_corpus():
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    # Check VERTEX_AI_LOCATION first, then GOOGLE_CLOUD_LOCATION, then default to us-central1
    # Note: RAG Engine has capacity limits in us-central1, so us-west1 is preferred for new projects
    location = os.getenv("VERTEX_AI_LOCATION") or os.getenv("GOOGLE_CLOUD_LOCATION", "us-west1")
    
    if not project_id:
        print("Error: GOOGLE_CLOUD_PROJECT not set in .env")
        return

    print(f"Initializing Vertex AI for project {project_id} in {location}...")
    vertexai.init(project=project_id, location=location)

    display_name = "Philosophy Classroom Corpus"
    
    try:
        print(f"Creating RAG Corpus: {display_name}...")
        # Note: rag.create_corpus might be the function, or rag.RagCorpus.create
        # Checking common patterns for preview features
        
        # Try creating via rag module directly if available
        corpus = rag.create_corpus(
            display_name=display_name,
            description="Knowledge base for Philosophy Classroom agents."
        )
        
        print(f"✅ Success! Corpus created.")
        print(f"Corpus Name: {corpus.name}") 
        print(f"\nPlease add this to your .env file:")
        print(f"PHILOSOPHY_CORPUS_ID={corpus.name}")
        
    except Exception as e:
        print(f"⚠️ Creation failed: {e}")
        
        try:
            print("Listing existing corpora...")
            corpora = rag.list_corpora()
            for c in corpora:
                if c.display_name == display_name:
                    print(f"Found existing corpus: {c.display_name}")
                    print(f"ID: {c.name}")
                    print(f"PHILOSOPHY_CORPUS_ID={c.name}")
                    return
        except Exception as list_e:
            print(f"Listing failed: {list_e}")

if __name__ == "__main__":
    setup_corpus()
