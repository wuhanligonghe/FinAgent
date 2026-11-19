"""Cookie and CSRF protocol. Authentication remains mandatory during tests."""
from fastapi import APIRouter, Request, Response
from pydantic import Field

from ..config import StrictModel
from .auth_store import AuthError, digest


class Login(StrictModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128, repr=False)
    remember: bool = False


class Register(StrictModel):
    username: str = Field(min_length=3, max_length=32)
    email: str | None = Field(None, max_length=254)
    display_name: str | None = Field(None, max_length=80)
    password: str = Field(min_length=15, max_length=128, repr=False)
    invite: str = Field(min_length=1, max_length=128, repr=False)


def cookie_name(settings):
    return '__Host-finsight_session' if settings.mode == 'server' else 'finsight_dev_session'


def set_cookie(response, settings, token, remember=False):
    response.set_cookie(cookie_name(settings), token, httponly=True, secure=settings.mode == 'server',
                        samesite='lax', path='/', max_age=settings.remember_seconds if remember else None)


def auth_response_error(request, exc):
    from fastapi.responses import JSONResponse
    headers = {'Retry-After': str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse({'code':exc.code, 'message':exc.message, 'request_id':request.state.request_id},
                        status_code=exc.status, headers=headers)


router = APIRouter(prefix='/api/v1/auth')


@router.get('/csrf')
def csrf(request: Request, response: Response):
    store = request.app.state.auth
    token = request.cookies.get(cookie_name(store.settings))
    if not store.resolve(token):
        store.limit('preauth:' + digest(request.client.host), 30, 60)
        token, _ = store.new_session()
        set_cookie(response, store.settings, token)
    return {'csrf_token':store.csrf(token), 'registration':store.settings.registration}


@router.post('/login')
def login(body: Login, request: Request, response: Response):
    store = request.app.state.auth
    user = store.authenticate(body.identifier, body.password, request.client.host, request.state.request_id)
    token, expires = store.new_session(user, body.remember, request.cookies.get(cookie_name(store.settings)))
    set_cookie(response, store.settings, token, body.remember)
    store.audit('login', user['id'], 'ok', request.state.request_id)
    return {'user':store.public(user), 'expires_at':expires, 'csrf_token':store.csrf(token)}


@router.get('/me')
def me(request: Request):
    session = request.state.auth_session
    if not session or not session['user_id']:
        raise AuthError('UNAUTHENTICATED', '请先登录')
    return {'user':{'id':session['user_id'], 'username':session['username_key'], 'display_name':session['display_name']},
            'expires_at':session['expires_at']}


@router.post('/logout', status_code=204)
def logout(request: Request):
    store = request.app.state.auth
    store.revoke(request.cookies.get(cookie_name(store.settings)))
    response = Response(status_code=204)
    response.delete_cookie(cookie_name(store.settings), path='/', secure=store.settings.mode == 'server', httponly=True, samesite='lax')
    return response


@router.post('/register', status_code=201)
def register(body: Register, request: Request):
    store = request.app.state.auth
    if store.settings.registration != 'invite':
        raise AuthError('REGISTRATION_CLOSED', '账户注册暂未开放', 403)
    store.limit('register:' + digest(request.client.host), 10, 3600)
    return {'user':store.create_user(body.username, body.password, body.email, body.display_name,
                                    invite=body.invite, require_invite=True)}
