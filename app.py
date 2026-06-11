import ipaddress
from flask import Flask,request,redirect,jsonify
from database import URLDatabase
import base62
import redis
from urllib.parse import urlparse
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app=Flask(__name__)

#initializing our primary database (SQLite)
db_repo=URLDatabase()

#initializing our cache (redis)
redis_client=redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True,
    protocol=2
    #decode_responses=True ensures redis gives us normal python strings instead of
    #raw b-strings (b'http...').
)

#initializing our rate limiter using Redis as storage
limiter=Limiter(
    get_remote_address,
    app=app,
    storage_uri='redis://localhost:6379/1?protocol=2',#using db 1 for rate limiting data,leaving db 0 for our url cache
    strategy='fixed-window'
)

def is_valid_url(url):
    try:
        parsed=urlparse(url)
        if parsed.scheme not in ('http','https'):
            return False
        
        #prevent SSRF (server side request forgery
        #occurs when an application fetches a user-supplied URL without properly validating or sanitizing it.
        #block local/private IPs (like 127.0.0.1 or 169.254.169.254)
        if parsed.hostname:
            try:
                ip=ipaddress.ip_address(parsed.hostname)
                #if ip is local or private raise ValueError
                if ip.is_loopback or ip.is_private:
                    return False
            except ValueError:
                pass
        return True
    except Exception as e:
        print(f"URL Validation Error: {e}")
        return False

@app.route('/shorten',methods=['POST'])
@limiter.limit('10 per minute') #limit url creation to prevent storage exhaustion
def shorten():
    data=request.get_json()
    long_url=data.get('long_url')

    if not long_url:
        return jsonify({'error':'missing url'}),400
    
    if not is_valid_url(long_url):
        return jsonify({'error':'Invalid or Unrestricted URL'}),400

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
    try:
        cached_url=redis_client.get(short_id)

        if(cached_url):
            print(f"Cache hit! Redirecting immediately.")
            redis_client.incr(f"clicks:{short_id}")
            return redirect(cached_url)

    except redis.RedisError as e:
        print(f"Redis is down or unreachable: {e}. Falling back to Database.")
    
    print(f"Cache miss (or Redis down) for {short_id}. Hitting DB...")
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
    try:
        redis_client.set(short_id,long_url,ex=86400)
        #ex=86400 sets a TTL (Time to Live) of 24 hrs. This automatically handles URL Cache Expiry
        #We also increment a counter for stats.
        redis_client.incr(f"clicks:{short_id}")
    
    except redis.RedisError as e:
        print(f"Failed to write to cache: {e}")

    #redirect
    return redirect(long_url)

if __name__ == "__main__":
    app.run(debug=True) 