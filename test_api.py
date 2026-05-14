import urllib.request
import json
import sys

def test_health():
    r = urllib.request.urlopen('http://localhost:8765/health')
    data = json.loads(r.read())
    print('=== Test 1: Health ===')
    print(f"Status: {data['status']}")
    print(f"Version: {data['version']}")
    print(f"Concurrency: {data['concurrency']}")
    print(f"Rate limit: {data['rate_limit']}")
    print(f"Models: {len(data['models'])} models")
    print()

def test_models():
    r = urllib.request.urlopen('http://localhost:8765/v1/models')
    data = json.loads(r.read())
    model_ids = [m['id'] for m in data['data']]
    print('=== Test 2: Models ===')
    print(f"doubao-image present: {'doubao-image' in model_ids}")
    print(f"doubao-podcast present: {'doubao-podcast' in model_ids}")
    print(f"doubao-expert present: {'doubao-expert' in model_ids}")
    expert = next(m for m in data['data'] if m['id'] == 'doubao-expert')
    print(f"doubao-expert capabilities: {expert['capabilities']}")
    print()

def test_delete():
    req = urllib.request.Request('http://localhost:8765/v1/conversations/test-nonexistent', method='DELETE')
    r = urllib.request.urlopen(req)
    data = json.loads(r.read())
    print('=== Test 3: Delete Conversation ===')
    print(f"Result: {data}")
    print()

def test_chat():
    req = urllib.request.Request(
        'http://localhost:8765/v1/chat/completions',
        data=json.dumps({
            'model': 'doubao-pro-chat',
            'messages': [{'role': 'user', 'content': '1+1=?'}],
            'stream': False
        }).encode(),
        headers={'Content-Type': 'application/json'}
    )
    r = urllib.request.urlopen(req, timeout=60)
    data = json.loads(r.read())
    print('=== Test 4: Basic Chat ===')
    content = data['choices'][0]['message']['content']
    print(f"Response length: {len(content)} chars")
    print(f"Content preview: {content[:100]}")
    print()

def test_image():
    req = urllib.request.Request(
        'http://localhost:8765/v1/chat/completions',
        data=json.dumps({
            'model': 'doubao-image',
            'messages': [{'role': 'user', 'content': '画一只可爱小猫'}],
            'stream': False
        }).encode(),
        headers={'Content-Type': 'application/json'}
    )
    r = urllib.request.urlopen(req, timeout=120)
    data = json.loads(r.read())
    print('=== Test 5: Image Model ===')
    content = data['choices'][0]['message']['content']
    has_image = '![' in content or 'image' in content.lower()
    print(f"Has image: {has_image}")
    print(f"Content preview: {content[:200]}")
    print()

if __name__ == '__main__':
    tests = sys.argv[1:] if len(sys.argv) > 1 else ['health', 'models', 'delete']
    for t in tests:
        try:
            globals()[f'test_{t}']()
        except Exception as e:
            print(f"Test {t} FAILED: {e}\n")
