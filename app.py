from flask import Flask, render_template, render_template_string, request, jsonify
from pymongo import MongoClient
import os
import datetime
import requests
from bs4 import BeautifulSoup
import openai
import json
import threading
import time
from dotenv import load_dotenv
from datasketch import MinHash, MinHashLSH

# --- 1. Configuration & Environment ---
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'vibgyor-secret'

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# --- 2. Database Connection (MongoDB) ---
DB_CONNECTED = False
try:
    # Adding TLS/SSL flags for environments with restrictive cert policies
    mongo_client = MongoClient(
        MONGO_URI, 
        serverSelectionTimeoutMS=5000, 
        tls=True, 
        tlsAllowInvalidCertificates=True
    )
    db = mongo_client["daily_news"]
    
    # Collections
    collection_raw = db["raw_articles"]
    collection_processed = db["processed_articles"]
    collection_users = db["users"]
    collection_circles = db["circles"]
    collection_engagements = db["engagements"]
    collection_posts = db["posts"]
    collection_notifications = db["notifications"]
    collection_meta = db["meta"]
    
    # Simple check
    mongo_client.admin.command('ping')
    DB_CONNECTED = True
    print("Successfully connected to MongoDB.")
except Exception as e:
    print(f"MongoDB Error: {e}. Falling back to mock data.")
    # Local JSON Fallback for "Real" information if DB fails
    LOCAL_DB_PATH = os.path.join(os.getcwd(), "local_news_db.json")
class MockCollection:
    def __init__(self, name, data=None):
        self.name = name
        self._data = data if data is not None else []
    def find(self, filter=None, *args, **kwargs):
        data = self._load()
        if filter:
            # Simple filter implementation
            data = [i for i in data if all(i.get(k) == v for k, v in filter.items())]
        return MockCollection(self.name, data)
    def sort( self, *args, **kwargs): return self
    def limit(self, n):
        return self._data[:n]
    def __iter__(self):
        return iter(self._data)
    def __getitem__(self, index):
        return self._data[index]
    def __len__(self):
        return len(self._data)
    def find_one(self, filter=None, *args, **kwargs):
        res = self.find(filter)
        return res[0] if len(res) > 0 else None
    def insert_one(self, doc):
        data = self._load()
        if '_id' not in doc: doc['_id'] = str(datetime.datetime.now().timestamp())
        data.append(doc)
        self._save(data)
        class Result: 
            def __init__(self, id): self.inserted_id = id
        return Result(doc['_id'])
    def insert_many(self, docs):
        data = self._load()
        for doc in docs:
            if '_id' not in doc: doc['_id'] = str(datetime.datetime.now().timestamp()) + "_" + str(docs.index(doc))
        data.extend(docs)
        self._save(data)
        class Result:
            def __init__(self, ids): self.inserted_ids = ids
        return Result([doc['_id'] for doc in docs])
    def update_one(self, filter, update, upsert=False):
        data = self._load()
        found = False
        for i, doc in enumerate(data):
            # Convert ObjectId to string for comparison if needed
            match = True
            for k, v in filter.items():
                val = doc.get(k)
                if str(val) != str(v): match = False; break
            if match:
                found = True
                if "$set" in update:
                    for k, v in update["$set"].items(): data[i][k] = v
                if "$inc" in update:
                    for k, v in update["$inc"].items(): data[i][k] = data[i].get(k, 0) + v
                break
        if not found and upsert:
            new_doc = filter.copy()
            if "$set" in update:
                for k, v in update["$set"].items(): new_doc[k] = v
            if "$inc" in update:
                for k, v in update["$inc"].items(): new_doc[k] = v
            if '_id' not in new_doc: new_doc['_id'] = str(datetime.datetime.now().timestamp())
            data.append(new_doc)
        self._save(data)
    def _load(self):
        try:
            filename = f"local_{self.name}.json"
            if os.path.exists(filename):
                with open(filename, "r") as f: 
                    return json.load(f)
        except Exception as e:
            print(f"MockDB Load Error: {e}")
        return []
    def _save(self, data):
        try:
            filename = f"local_{self.name}.json"
            with open(filename, "w") as f: 
                json.dump(data, f)
        except: pass

if not DB_CONNECTED:
    collection_raw = MockCollection("raw")
    collection_processed = MockCollection("processed")
    collection_users = MockCollection("users")
    collection_circles = MockCollection("circles")
    collection_engagements = MockCollection("engagements")
    collection_posts = MockCollection("posts")
    collection_notifications = MockCollection("notifications")
    collection_meta = MockCollection("meta")

@app.template_filter('from_json')
def from_json_filter(s):
    try:
        return json.loads(s)
    except:
        return {}

COLORS = {
    "red": "#E53935",     # Arts & Culture
    "orange": "#FB8C00",  # Self & Well-being
    "yellow": "#FBC02D",  # Business & Economy
    "green": "#43A047",   # The Natural World
    "blue": "#1E88E5",    # Society & Governance
    "indigo": "#3949AB",  # Science & Technology
    "violet": "#8E24AA",  # Philosophy & Belief
    "white": "#FFFFFF",   # User
    "black": "#000000"    # Dark mode
}

CAT_MAP = {
    # Colors to Slugs
    "red": "arts",
    "orange": "selfwell",
    "yellow": "economy",
    "green": "nature",
    "blue": "society",
    "indigo": "tech",
    "violet": "philo",
    
    # Full Names to Slugs (for Tribe/Circles)
    "society & governance": "society",
    "business & economy": "economy",
    "science & tech": "tech",
    "arts & culture": "arts",
    "natural world": "nature",
    "self & well-being": "selfwell",
    "philosophy & belief": "philo",
    
    # Common variations
    "society": "society",
    "economy": "economy",
    "tech": "tech",
    "arts": "arts",
    "nature": "nature",
    "selfwell": "selfwell",
    "philo": "philo"
}

# Removed SQLAlchemy models in favor of MongoDB collections.
# Schema is now dynamic but follows the structure defined in the implementation plan.

# --- 3. AI & Data Services ---
def process_with_ai(title, content):
    if not OPENAI_API_KEY:
        return {
            "headline": "API Configuration Required",
            "category": "orange",
            "summary": "Please configure your OpenAI API Key in the .env file to enable AI-driven deep analysis and 60-word summaries.",
            "summaries": {},
            "fake_news_score": 0.0
        }
    
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""
    You are a professional news editor for a mobile app similar to Way2News.
    Analyze the following news article and provide a high-intel summary and translations.

    Requirements:
    1. Generate an attractive, short headline (max 10 words).
    2. The summary MUST be exactly between 60 and 63 words long. Use professional and engaging tone.
    3. Categorize it into one of: (arts, selfwell, economy, nature, society, tech, philo).
       - arts: Arts & Culture, selfwell: Self & Well-being, economy: Business & Economy, nature: The Natural World, society: Society & Governance, tech: Science & Tech, philo: Philosophy & Belief
    4. Provide translations for the headline and summary in: Hindi, Telugu, Tamil.
    5. Evaluate the "Fake News Score" from 0.0 (Reliable) to 1.0 (Likely Fake) based on the content and common knowledge.

    Return the result in strictly JSON format:
    {{
        "headline": "...",
        "category": "...",
        "summary": "...",
        "translations": {{
            "hindi": {{"headline": "...", "summary": "..."}},
            "telugu": {{"headline": "...", "summary": "..."}},
            "tamil": {{"headline": "...", "summary": "..."}}
        }},
        "fake_news_score": 0.0
    }}

    Article Title: {title}
    Article Content: {content[:1500]}
    """
    try:
        response = client.chat.completions.create(
            # Using gpt-4o as per the existing code or gpt-4o-mini if available
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        data = json.loads(response.choices[0].message.content)
        return data
    except Exception as e:
        return {
            "headline": "Real-Time Update",
            "category": "blue",
            "summary": "Full analysis pending. Coverage available via source link.",
            "translations": {},
            "fake_news_score": 0.0
        }

def fetch_search_news(query):
    articles = []
    if NEWS_API_KEY:
        url = f"https://newsapi.org/v2/everything?q={query}&sortBy=popularity&apiKey={NEWS_API_KEY}&language=en"
        try:
            r = requests.get(url, timeout=12)
            api_articles = r.json().get("articles", [])
            for a in api_articles[:15]: # Limit to top 15 results for on-demand search
                articles.append({
                    "source": a["source"]["name"],
                    "external_id": a["url"],
                    "title": a["title"],
                    "content": a["description"] or a["content"] or "Full coverage available via source link.",
                    "url": a["url"],
                    "image_url": a.get("urlToImage"),
                    "published_at": a.get("publishedAt") or datetime.datetime.utcnow().isoformat(),
                    "location": "global",
                    "query": query
                })
        except: pass
    return articles

def fetch_latest_news(country='in'):
    articles = []
    
    # 1. API Fetch (Location-based)
    if NEWS_API_KEY:
        # Fetch global news + location specific news
        urls = [
            f"https://newsapi.org/v2/top-headlines?apiKey={NEWS_API_KEY}&language=en",
            f"https://newsapi.org/v2/top-headlines?apiKey={NEWS_API_KEY}&country={country}"
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                api_articles = r.json().get("articles", [])
                for a in api_articles:
                    articles.append({
                        "source": a["source"]["name"],
                        "external_id": a["url"],
                        "title": a["title"],
                        "content": a["description"] or a["content"] or "Full coverage available via source link.",
                        "url": a["url"],
                        "image_url": a.get("urlToImage"),
                        "published_at": datetime.datetime.utcnow().isoformat(),
                        "location": country if 'country=' in url else 'global'
                    })
            except: pass

    # 2. Targeted Web Scraping
    # ... (rest of scraping remains similar but simplified for brevity)
    return articles

# --- 4. Main Logic ---
lsh = MinHashLSH(threshold=0.8, num_perm=128)

def sync_news(location='in'):
    articles = fetch_latest_news(location)
    process_articles_batch(articles)
    collection_meta.update_one({"id": "last_sync"}, {"$set": {"time": datetime.datetime.utcnow().isoformat()}}, upsert=True)

def sync_search_query(query):
    articles = fetch_search_news(query)
    process_articles_batch(articles)
    collection_meta.update_one({"id": f"sync_{query}"}, {"$set": {"time": datetime.datetime.utcnow().isoformat()}}, upsert=True)

def process_articles_batch(articles):
    for item in articles:
        try:
            m = MinHash(num_perm=128)
            for d in item["title"].lower().split(): m.update(d.encode('utf8'))
            if lsh.query(m): continue
            
            # Check for duplicate
            if collection_raw.find_one({"external_id": item["external_id"]}):
                continue
                
            res = collection_raw.insert_one(item)
            raw_id = res.inserted_id
            
            ai_data = process_with_ai(item["title"], item["content"])
            
            # Ensure we use REAL info if AI synthesis failed
            headline = ai_data.get("headline")
            if not headline or headline == "Real-Time Update":
                headline = item["title"]
            
            summary = ai_data.get("summary")
            if not summary or "Full analysis pending" in summary:
                summary = item["content"][:280] + "..." if len(item["content"]) > 280 else item["content"]

            processed = {
                "id": str(raw_id),
                "raw_id": str(raw_id),
                "external_id": item["external_id"],
                "source": item["source"],
                "headline": headline,
                "summary": summary,
                "summaries_json": json.dumps(ai_data.get("translations", {})),
                "category": ai_data.get("category", "orange").lower(),
                "image_url": item["image_url"] or "https://loremflickr.com/800/600/news?random=" + str(raw_id),
                "fake_news_score": ai_data.get("fake_news_score", 0.0),
                "created_at": datetime.datetime.utcnow().isoformat(),
                "location": item.get("location", "global"),
                "url": item["url"]
            }
            collection_processed.insert_one(processed)
            lsh.insert(str(raw_id), m)
            
            # Create notification
            notif = {
                "title": f"New Story: {processed['headline'][:40]}...",
                "body": processed['summary'][:100],
                "img": processed['image_url'],
                "time": "Just now",
                "art_id": processed['id'],
                "created_at": datetime.datetime.utcnow().isoformat()
            }
            collection_notifications.insert_one(notif)
        except Exception as e:
            print(f"Error syncing item: {e}")


@app.route('/signin')
def signin_page():
    return render_page('signin')

@app.route('/onboarding')
def onboarding_page():
    return render_page('onboarding')

@app.route('/')
def index():
    return render_page('home')

@app.route('/explore')
def explore():
    return render_page('explore')

@app.route('/community')
def community_page():
    return render_page('community')

@app.route('/profile')
def profile_page():
    return render_page('profile')

def render_page(active_view='home', user=None):
    from bson import ObjectId
    
    # 1. Fetch News
    articles_raw = list(collection_processed.find().sort("created_at", -1).limit(40))
    articles = []
    for a in articles_raw:
        db_cat = a.get("category", "society").lower()
        mapped_cat = CAT_MAP.get(db_cat, db_cat)
        mapped = {
            "id": str(a.get("id", a.get("_id"))),
            "cat": mapped_cat,
            "src": a.get("source", "SherByte Intel"),
            "title": a.get("headline", "Global Update"),
            "img": a.get("image_url", "https://loremflickr.com/800/600/news?random=" + str(a.get("id", a.get("_id")))),
            "text": a.get("summary", ""),
            "breaking": a.get("fake_news_score", 0) < 0.2, 
            "summaries": json.loads(a["summaries_json"]) if a.get("summaries_json") else {}
        }
        articles.append(mapped)

    # Fallback to internal mocks ONLY if even local JSON is empty and no articles found
    if not articles:
        articles = [
            {"id": "mock1", "cat": "tech", "src": "SherByte", "title": "Syncing Real News...", "img": "https://loremflickr.com/800/600/tech?random=1", "text": "We are currently analyzing the world's pulse. Real news will appear shortly.", "breaking": True}
        ]

    # 2. Fetch Tribe (Community) Data
    if not DB_CONNECTED:
        tribe_data = [
            {"id": "t1", "cat": "tech", "user": "@tester", "ava": "https://loremflickr.com/100/100/face?random=1", "time": "1m", "title": "Check this out", "text": "Tribe posts are working!", "likes": 5, "comments": []}
        ]
    else:
        posts_raw = list(collection_posts.find().sort("created_at", -1).limit(20))
        tribe_data = []
        for p in posts_raw:
            p_user = collection_users.find_one({"_id": p["user_id"]})
            handle = p_user["handle"] if p_user else "@anonymous"
            ava_url = f"https://loremflickr.com/100/100/face?random={str(p['user_id'])}"
            
            raw_cat = p.get("circle_name", "@society").replace('@', '').lower()
            mapped_cat = CAT_MAP.get(raw_cat, raw_cat)
            
            tribe_data.append({
                "id": str(p["_id"]),
                "cat": mapped_cat,
                "user": handle,
                "ava": ava_url,
                "time": "Just now", # Placeholder
                "title": p.get("title", "Community Update"),
                "img": p.get("image_url"),
                "likes": p.get("likes", 0),
                "comments": [] # Placeholder for now
            })
    # 3. Notifications
    notifications_raw = list(collection_notifications.find().sort("created_at", -1).limit(10))
    notifications = []
    for n in notifications_raw:
        notifications.append({
            "img": n.get("img", ""),
            "title": n.get("title", "Breaking News"),
            "time": n.get("time", "Just now"),
            "art_id": n.get("art_id")
        })

    if not notifications:
        notifications = [
            {"img": "https://loremflickr.com/100/100/news?random=1", "title": "SherByte Neural Link Active", "time": "Just now"}
        ]

    return render_template('index.html', 
                           articles=articles, 
                           tribe_data=tribe_data, 
                           notifications=notifications,
                           active_view=active_view,
                           user=user)


# --- A/B/C Recommendation Engine Logic ---
def get_personalized_categories(user_id):
    user = collection_users.find_one({"_id": user_id})
    if not user: return []
    
    # Step 1: Cold Start (Explicit choices)
    my_circles = user.get("joined_circles", [])
    
    # Step 2: Collaborative Filtering (A/B/C)
    all_users = list(collection_users.find({"_id": {"$ne": user_id}}))
    predicted = set()
    for other in all_users:
        other_circles = other.get("joined_circles", [])
        common = set(my_circles).intersection(set(other_circles))
        if len(common) >= 2: # High similarity
            diff = set(other_circles) - set(my_circles)
            predicted.update(diff)
            
    # Step 3: Engagement Refinement
    engagements = list(collection_engagements.find({"user_id": user_id}))
    for eng in engagements:
        if (eng.get("score", 0) + (eng.get("watch_time", 0) / 5.0)) >= 5.0:
            predicted.add(f"@{eng['category'].capitalize()}")
        elif (eng.get("score", 0) + (eng.get("watch_time", 0) / 5.0)) < -2.0:
            tag = f"@{eng['category'].capitalize()}"
            if tag in predicted: predicted.remove(tag)
            
    final_categories = list(set(my_circles).union(predicted))
    return [c.replace('@', '') for c in final_categories]

@app.route('/api/feed/<string:user_id>')
def personalized_feed(user_id):
    from bson import ObjectId
    categories = get_personalized_categories(ObjectId(user_id))
    articles = list(collection_processed.find({
        "category": {"$in": [c.lower() for c in categories]}
    }).sort("created_at", -1).limit(30))
    
    return jsonify([{
        "id": str(a["_id"]), "summary": a["summary"], "category": a["category"],
        "image_url": a["image_url"], "created_at": a["created_at"].isoformat()
    } for a in articles])

@app.route('/api/onboard', methods=['POST'])
def onboard_user():
    data = request.json # { "handle": "...", "circles": ["@Tech", ...] }
    user = collection_users.find_one({"handle": data['handle']})
    if not user:
        user_data = {
            "handle": data['handle'],
            "display_name": data['handle'].replace('@', ''),
            "joined_circles": data['circles'],
            "karma": 0,
            "joined_at": datetime.datetime.utcnow()
        }
        user_id = collection_users.insert_one(user_data).inserted_id
    else:
        user_id = user["_id"]
        collection_users.update_one({"_id": user_id}, {"$set": {"joined_circles": data['circles']}})
    
    return jsonify({"status": "Onboarded", "user_id": str(user_id)})

@app.route('/api/news/<string:art_id>')
def get_article_detail(art_id):
    from bson import ObjectId
    try:
        a = collection_processed.find_one({"_id": ObjectId(art_id)})
        if not a:
            return jsonify({"error": "Article not found"}), 404
        
        # Get raw article for source link and deep content
        raw = collection_raw.find_one({"_id": a["raw_id"]})
        if not raw:
            # Try finding by external_id if raw_id is string/id mismatch
            raw = collection_raw.find_one({"external_id": a.get("external_id")})
        
        return jsonify({
            "id": str(a["_id"]),
            "headline": a["headline"],
            "summary": a["summary"],
            "category": a["category"],
            "image_url": a["image_url"],
            "source": a.get("source", "SherByte Intel"),
            "url": a.get("url") or (raw.get("url") if raw else "#"),
            "content": raw.get("content", "Deep analysis complete. Full coverage available via source link.") if raw else a["summary"],
            "translations": json.loads(a.get("summaries_json", "{}")),
            "fake_news_score": a.get("fake_news_score", 0.0)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/notifications')
def get_live_notifications():
    if not DB_CONNECTED:
        return jsonify([
            {"img": "https://loremflickr.com/100/100/news?random=1", "title": "Mock: News Scanner Offline", "time": "Just now"},
            {"img": "https://loremflickr.com/100/100/city?random=2", "title": "Mock: Check Database Settings", "time": "1m ago"}
        ])
    
    notifications_raw = list(collection_notifications.find().sort("created_at", -1).limit(10))
    notifications = []
    for n in notifications_raw:
        notifications.append({
            "img": n.get("img", ""),
            "title": n.get("title", "Breaking News"),
            "time": n.get("time", "Just now")
        })
    return jsonify(notifications)

@app.route('/bytes')
def bytes_page():
    articles = list(collection_processed.find().sort("created_at", -1).limit(20))
    # Hook Injection: Every 5th article is random/discovery
    import random
    all_processed = list(collection_processed.find())
    if len(all_processed) > 5:
        mixed_articles = []
        for i, art in enumerate(articles):
            mixed_articles.append(art)
            if (i + 1) % 5 == 0:
                hook = random.choice(all_processed)
                if hook not in mixed_articles:
                    mixed_articles.append(hook)
        articles = mixed_articles
    return render_page('bytes', articles=articles)

@app.route('/api/circles/create', methods=['POST'])
def create_circle():
    data = request.json # { "name": "@NewTopic", "color": "#HEX", "founders": [uid1, uid2] }
    founders = data.get('founders', [])
    if len(founders) < 2:
        return jsonify({"error": "Two-person rule: A new circle requires at least 2 founding members."}), 400
    
    if collection_circles.find_one({"name": data['name']}):
        return jsonify({"error": "Circle already exists."}), 400
        
    circle_data = {
        "name": data['name'],
        "color": data.get('color', '#FFFFFF'),
        "description": data.get('description', 'User Created Tribe'),
        "founders": founders
    }
    collection_circles.insert_one(circle_data)
    
    return jsonify({"status": "Circle Created", "circle": data['name']}), 201

@app.route('/api/posts/create', methods=['POST'])
def create_post():
    from bson import ObjectId
    data = request.json # { "user_id": "...", "circle_name": "@Tech", "content": "..." }
    if not data.get('circle_name'):
        return jsonify({"error": "Mandatory Circle Selection required."}), 400
        
    post_data = {
        "user_id": ObjectId(data['user_id']),
        "circle_name": data['circle_name'],
        "content": data['content'],
        "created_at": datetime.datetime.utcnow(),
        "likes": 0
    }
    post_id = collection_posts.insert_one(post_data).inserted_id
    
    # Increase karma for activity
    collection_users.update_one({"_id": ObjectId(data['user_id'])}, {"$inc": {"karma": 1}})
    
    return jsonify({"status": "Post Created", "post_id": str(post_id)}), 201

@app.route('/api/engage', methods=['POST'])
def track_engagement():
    from bson import ObjectId
    data = request.json # { "user_id": "...", "category": "blue", "type": "click|watch", "value": 1.0 }
    user_id = ObjectId(data['user_id'])
    
    update = {"$inc": {}}
    if data.get('type') == 'click':
        update["$inc"]["score"] = 1.0
    elif data.get('type') == 'watch':
        update["$inc"]["score"] = (data.get('value', 0) / 5.0)
    elif data.get('type') == 'skip':
        update["$inc"]["score"] = -2.0
        
    collection_engagements.update_one(
        {"user_id": user_id, "category": data['category']},
        update,
        upsert=True
    )
    return jsonify({"status": "Engagement Tracked"}), 200

@app.route('/sync', methods=['POST'])
def trigger_sync():
    sync_news()
    return jsonify({"status": "World Analysis Completed Successfully"})

@app.route('/api/news')
def get_news_api():
    articles = list(collection_processed.find().sort("created_at", -1).limit(50))
    return jsonify([{
        "id": str(a["_id"]),
        "summary": a["summary"],
        "category": a["category"],
        "image_url": a["image_url"],
        "created_at": a["created_at"].isoformat()
    } for a in articles])

@app.route('/webhook', methods=['POST'])
def webhook_receiver():
    """
    Universal Webhook Endpoint.
    Accepts: {"title": "...", "content": "...", "source": "...", "image_url": "..."}
    """
    data = request.json
    if not data or 'title' not in data or 'content' not in data:
        return jsonify({"error": "Invalid news payload"}), 400
    
    # Process for the feed
    item = {
        "source": data.get("source", "Webhook Intel"),
        "external_id": f"webhook_{datetime.datetime.utcnow().timestamp()}",
        "title": data["title"],
        "content": data["content"],
        "url": data.get("url", "#"),
        "image_url": data.get("image_url"),
        "published_at": datetime.datetime.utcnow()
    }
    
    # Simple Deduplication
    m = MinHash(num_perm=128)
    for d in item["title"].lower().split(): m.update(d.encode('utf8'))
    if lsh.query(m):
        return jsonify({"status": "Duplicate ignored"}), 200

    try:
        raw_id = collection_raw.insert_one(item).inserted_id
        
        ai_data = process_with_ai(item["title"], item["content"])
        processed = {
            "raw_id": raw_id,
            "headline": ai_data.get("headline", item["title"][:255]),
            "summary": ai_data.get("summary"),
            "category": ai_data.get("category").lower(),
            "image_url": item.get("image_url"),
            "fake_news_score": ai_data.get("fake_news_score", 0.0),
            "created_at": datetime.datetime.utcnow()
        }
        collection_processed.insert_one(processed)
        lsh.insert(item["external_id"], m)
        return jsonify({"status": "Webhook AI Analysis Integrated"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- 6. Background Engine ---
def background_scanner():
    """Periodically scans for fresh world news automatically."""
    while True:
        try:
            with app.app_context():
                print("--- AUTOMATIC AI SCAN STARTED ---")
                sync_news()
                print("--- AUTOMATIC AI SCAN COMPLETED ---")
        except Exception as e:
            print(f"Scanner Error: {e}")
        time.sleep(900) # Scan every 15 minutes

# --- Initial Seeding ---
def seed_db():
    if collection_circles.find_one(): return
    
    # Comprehensive VIBGYOR Topics from Requirements
    vibgyor_data = {
        "Society & Governance": {
            "color": "#1E88E5",
            "topics": ["Political Ideologies", "Global Affairs", "National Politics", "Law & Justice", "Urban & Civic Life", "Education & Academia", "Activism & Movements", "Social Sciences"]
        },
        "Business & Economy": {
            "color": "#FBC02D",
            "topics": ["Markets & Investing", "Corporate World", "Startups & Venture Capital", "Macroeconomics", "Marketing & Trends", "Real Estate", "Business Tech", "Insurance & Risk"]
        },
        "Science & Tech": {
            "color": "#3949AB",
            "topics": ["Artificial Intelligence", "Software Engineering", "Hardware & DIY", "Internet Culture", "Future Tech", "Space & Physics", "Mathematics & Logic", "Engineering"]
        },
        "Arts & Culture": {
            "color": "#E53935",
            "topics": ["Aesthetics", "Hobbies & Crafts", "Fashion & Style", "Cinema & Visuals", "Music & Audio", "Gaming & Esports", "Literature", "Subcultures"]
        },
        "Natural World": {
            "color": "#43A047",
            "topics": ["Gardening & Plants", "Animals & Zoology", "Environment", "Geography & Travel", "Biology", "Natural Phenomena"]
        },
        "Self & Well-being": {
            "color": "#FB8C00",
            "topics": ["Fitness & Sport", "Mental Health", "Nutrition & Food", "Lifestyle & Home", "Parenting & Family", "Relationships", "Productivity"]
        },
        "Philosophy & Belief": {
            "color": "#8E24AA",
            "topics": ["Spiritual Practices", "Philosophy Schools", "Religion & Theology", "Esoteric & Occult", "Ethics & Morality", "Consciousness", "Mythology"]
        }
    }

    circles_to_insert = []
    for category, info in vibgyor_data.items():
        # Seed parent-like circles for each VIBGYOR main category
        circles_to_insert.append({
            "name": f"@{category.replace(' ', '')}",
            "color": info["color"],
            "description": f"Main category: {category}"
        })
        
        # Seed individual sub-topic circles
        for topic in info["topics"]:
            circles_to_insert.append({
                "name": f"@{topic.replace(' ', '')}",
                "color": info["color"],
                "description": f"Focus: {topic}"
            })
            
    if circles_to_insert:
        collection_circles.insert_many(circles_to_insert)
    
    # Proactive news sync if empty
    if not collection_processed.find_one():
        print("--- NO NEWS DETECTED. TRIGGERING INITIAL SYNC ---")
        threading.Thread(target=sync_news, daemon=True).start()
    
    print("--- SHERBYTE VIBGYOR CIRCLES SEEDED IN MONGODB ---")

@app.route('/api/news/location/<string:loc>')
def get_location_news(loc):
    # Fetch news for specific location
    articles = list(collection_processed.find({"location": loc}))
    if not articles:
        # If no articles for this location, trigger a sync and check again
        sync_news(loc)
        articles = list(collection_processed.find({"location": loc}))
    return jsonify(articles[:20])

@app.route('/api/feed')
def get_feed():
    loc = request.args.get('location', 'global')
    # Merge global and local news
    local_news = list(collection_processed.find({"location": loc}))
    global_news = list(collection_processed.find({"location": "global"}))
    merged = local_news + global_news
    # Sort by date
    merged.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    
    last_sync = collection_meta.find_one({"id": "last_sync"})
    sync_time = last_sync["time"] if last_sync else None
    
    return jsonify({
        "articles": merged[:30],
        "last_sync": sync_time
    })

@app.route('/api/sync/search')
def trigger_search_sync():
    q = request.args.get('q')
    if not q: return jsonify({"error": "No query provided"}), 400
    # Process in foreground for real-time feel since it's only 15 articles
    sync_search_query(q)
    # Return limited results for this query specifically
    results = list(collection_processed.find({"query": q}))
    results.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return jsonify(results[:15])

# --- Initial Seeding & Startup ---
# In serverless (Vercel), __name__ is not "__main__", so we run initialization here
# but wrap in a simple check to avoid double-running if possible.
_initialized = False
def initialize_app():
    global _initialized
    if _initialized: return
    seed_db()
    if not os.environ.get('VERCEL'):
        print("--- STARTING BACKGROUND SCANNER ---")
        scanner_thread = threading.Thread(target=background_scanner, daemon=True)
        scanner_thread.start()
    _initialized = True

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
