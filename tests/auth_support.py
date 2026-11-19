"""Explicit test authentication, never a production authentication bypass."""
PASSWORD = 'synthetic-test-password-2026'


def login(client, username='alice', password=PASSWORD):
    client.headers['Origin'] = 'http://testserver'
    csrf = client.get('/api/v1/auth/csrf').json()['csrf_token']
    result = client.post('/api/v1/auth/login', json={'identifier': username, 'password': password}, headers={'X-CSRF-Token':csrf})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf_token']
    client.headers['X-FinSight-User'] = result.json()['user']['id']
    return result.json()['user']
