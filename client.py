import requests

print('URL Shortener CLI')
print('Make sure Flask server is running in another terminal')

while True:
    long_url=input("Enter long URL or type 'q' to quit: ").strip()
    if long_url.lower() == 'q':
        print("Exiting...")
        break
    if not long_url:
        continue
    try:
        #send post request to Flask API
        response=requests.post(
            "http://127.0.0.1:5000/shorten",
            json={'long_url':long_url}
        )
        #parse JSON response
        data = response.json()
        if response.status_code == 201:
            print(f"\n\nSuccessfully Shortened!\nShortened URL:{data['short_url']}\n")
        else:
            print(f"Error:{data.get('error')}")
        
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to Flask server at http://127.0.0.1:5000.")