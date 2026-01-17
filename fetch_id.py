import requests
import json

# The part of the URL after "event/"
slug = "bitcoin-above-on-january-13" 

print(f"--- FETCHING DATA FOR: {slug} ---")
url = f"https://gamma-api.polymarket.com/events?slug={slug}"

try:
    response = requests.get(url)
    data = response.json()

    if data:
        event = data[0]
        print(f"Event: {event.get('title')}")
        
        print("\n--- MARKET LIST ---")
        for market in event.get('markets', []):
            question = market.get('question')
            # TRY BOTH KEY FORMATS
            c_id = market.get('conditionId') or market.get('condition_id')
            
            print(f"Question: {question}")
            print(f"CONDITION ID: {c_id}") 
            print("-" * 30)
    else:
        print("Market not found via API.")

except Exception as e:
    print(f"Error: {e}")