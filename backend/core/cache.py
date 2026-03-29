import time
from sentence_transformers import SentenceTransformer
import numpy as np
import time

model = SentenceTransformer("all-MiniLM-L6-v2")
CACHE = []

def cosine_similarity(a,b):
    return np.dot(a,b) / (np.linalg.norm(a)*np.linalg.norm(b))


def get_ttl(query):
    query = query.lower()

    if "weather" in query:
        return 600      # 10 minutes

    if "nearby" in query or "restaurants" in query:
        return 86400    # 1 day

    return 3600         # default 1 hour


def get_cache(query, threshold=0.85):
    query_embedding = model.encode(query)

    best_match = None
    best_score = 0

    for item in CACHE:
        # check expiry
        if item["expiry"] and item["expiry"] < time.time():
            continue

        score = cosine_similarity(query_embedding, item["embedding"])

        if score > best_score:
            best_score = score
            best_match = item

    if best_score >= threshold:
        print(f"[SMART CACHE HIT] similarity={best_score:.2f}")
        return best_match["answer"]

    print("[SMART CACHE MISS]")
    return None


   
def set_cache(query, answer, ttl=3600):
    embedding = model.encode(query)
    expiry = time.time() + ttl if ttl else None

    CACHE.append({
        "question": query,
        "embedding": embedding,
        "answer": answer,
        "expiry": expiry
    })

    print("[SMART CACHE SAVED]")
