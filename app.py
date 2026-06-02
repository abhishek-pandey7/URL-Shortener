from flask import Flask,request,redirect,jsonify
from database import URLDatabase
import base62
import redis

app=Flask(__name__)

#initializing our primary database (SQLite)
db_repo=URLDatabase()

#initializing our cache (redis)
redis_client=redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
    #decode_responses=True ensures redis gives us normal python strings instead of
    #raw b-strings (b'http...').
)

@app.route('/shorten',methods=['POST'])
def shorten():
    data=request.get_json()
    long_url=data.get('long_url')

    if not long_url:
        return jsonify({'error':'missing url'}),400
    
    #database write: get autoincrementing counter id
    db_id=db_repo.insert(long_url)

    #base62 encode the counterid
    short_id=base62.encode(db_id)
    short_url=request.host_url+short_id

    #we do not add to redis right now because we use Cache Aside(Lazy Loading) strategy
    #aves Redis memory. If a user creates 1,000 links but nobody ever clicks them, 
    #we didn't waste RAM holding them.

    return jsonify({"short_url": short_url}), 201

@app.route('/<short_id>')
def redirect_url(short_id):
    #Cache-Aside pattern with Redis
    #1. check redis first
    cached_url=redis_client.get(short_id)

    if(cached_url):
        print(f"Cache hit! Redirecting immediately.")
        redis_client.incr(f"clicks:{short_id}")
        return redirect(cached_url)

    print(f"Cache miss for {short_id}. Hitting SQLite...")    

    #2. cache miss : decode string to integer ID
    try:
        db_id=base62.decode(short_id)
    except ValueError:
        return jsonify({'error':'Invalid URL Format'}),400
    
    #fetch from primary database (slow path)
    long_url=db_repo.get_by_id(db_id)

    if not long_url:
        return jsonify({'error':'URL not found'}),404
    
    #4. populate redis cache for next user
    redis_client.set(short_id,long_url,ex=86400)
    #ex=86400 sets a TTL (Time to Live) of 24 hrs. This automatically handles URL Cache Expiry
    #We also increment a counter for stats.
    redis_client.incr(f"clicks:{short_id}")

    #redirect
    return redirect(long_url)

if __name__ == "__main__":
    app.run(debug=True) 