import traceback
from flask import Flask, make_response, render_template, request, redirect, session
import mastodon
from misskey import Misskey, MiAuth, Permissions
from misskey.exceptions import *
import random
from datetime import timedelta
import urllib.parse
import sqlite3
import re
import MeCab
import markovify
import config
import requests
import uuid
import hashlib

def dict_factory(cursor, row):
   d = {}
   for idx, col in enumerate(cursor.description):
       d[col[0]] = row[idx]
   return d

def format_text(t):
    t = t.replace('　', ' ')  # Full width spaces
    # t = re.sub(r'([。．！？…]+)', r'\1\n', t)  # \n after ！？
    t = re.sub(r'(.+。) (.+。)', r'\1 \2\n', t)
    t = re.sub(r'\n +', '\n', t)  # Spaces
    t = re.sub(r'([。．！？…])\n」', r'\1」 \n', t)  # \n before 」
    t = re.sub(r'\n +', '\n', t)  # Spaces
    t = re.sub(r'\n+', r'\n', t).rstrip('\n')  # Empty lines
    t = re.sub(r'\n +', '\n', t)  # Spaces
    return t

db = sqlite3.connect('markov.db', check_same_thread=False)
db.row_factory = dict_factory

app = Flask(__name__)
# ランダムバイトから鍵生成
app.secret_key = bytes(bytearray(random.getrandbits(8) for _ in range(32)))
app.permanent_session_lifetime = timedelta(hours=1)

@app.route('/')
def root():
    return render_template('index.html')

@app.route('/login', methods=['POST'])
def login():
    data = request.form
    if not data.get('type'):
        return make_response('type is required', 400)
    if not data.get('hostname'):
        return make_response('hostname is required', 400)
    
    if data['type'] == 'misskey':
        session['logged_in'] = False
        session.permanent = True
        session['hostname'] = data['hostname']
        session['type'] = data['type']
        mi = Misskey(address=data['hostname'])
        instance_info = mi.meta()

        if instance_info['features'].get('miauth') == True:
            miauth = MiAuth(address=data['hostname'], name='markov-generator-fedi', callback=f'{request.host_url}login/callback', permission=[Permissions.READ_ACCOUNT])
            url = miauth.generate_url()
            session['session_id'] = miauth.session_id
            session['mi_legacy'] = False
            return redirect(url)
        else:
            # v12.39.1以前のインスタンス向け

            options = {
                'name': 'markov-generator-fedi (Legacy)',
                'callback': f'{request.host_url}login/callback',
                'permission': ['read:account'],
                'description': 'Created by CyberRex (@cyberrex_v2@misskey.io)',
                'callbackUrl': f'{request.host_url}login/callback',
            }

            r = requests.post(f'https://{data["hostname"]}/api/app/create', json=options)
            if r.status_code != 200:
                return make_response(f'Failed to generate app: {r.text}', 500)
            j = r.json()

            secret_key = j['secret']

            r = requests.post(f'https://{data["hostname"]}/api/auth/session/generate', json={'appSecret': secret_key})
            if r.status_code != 200:
                return make_response(f'Failed to generate session: {r.text}', 500)
            j = r.json()
            
            session['mi_session_token'] = j['token']
            session['mi_secret_key'] = secret_key
            session['mi_legacy'] = True
            return redirect(j['url'])
    
    if data['type'] == 'mastodon':
        session['logged_in'] = False
        session.permanent = True
        session['hostname'] = data['hostname']
        session['type'] = data['type']
        
        #client = mastodon.Mastodon(api_base_url=f'https://{data["hostname"]}')
        #app = client.create_app(client_name='markov-generator-fedi', redirect_uris=[f'{request.host_url}login/callback'], scopes=['read'])
        
        
        options = {
            'client_name': 'markov-generator-fedi',
            'redirect_uris': f'{request.host_url}login/callback',
            'scopes': 'read'
        }
        r = requests.post(f'https://{data["hostname"]}/api/v1/apps', json=options)
        if r.status_code != 200:
            return make_response(f'Failed to regist app: {r.text}', 500)
        d = r.json()
        session['mstdn_app_key'] = d['client_id']
        session['mstdn_app_secret'] = d['client_secret']
        session['mstdn_redirect_uri'] = f'{request.host_url}login/callback'

        querys = {
            'client_id': d['client_id'],
            'response_type': 'code',
            'redirect_uri': f'{request.host_url}login/callback',
            'scope': 'read',
        }

        return redirect(f'https://{data["hostname"]}/oauth/authorize?{urllib.parse.urlencode(querys)}')
    
    return 'How did you come to here'
    
@app.route('/login/callback')
def login_msk_callback():
    if not ('logged_in' in list(session.keys())):
        return make_response('<meta name="viewport" content="width=device-width">セッションデータが異常です。Cookieを有効にしているか確認の上再試行してください。<a href="/">トップページへ戻る</a>', 400)

    if session['type'] == 'misskey':

        if not session['mi_legacy']:

            miauth = MiAuth(session['hostname'] ,session_id=session['session_id'])
            try:
                token = miauth.check()
            except MisskeyMiAuthFailedException:
                session.clear()
                return make_response('<meta name="viewport" content="width=device-width">認証に失敗しました。', 500)
            session['token'] = token

        else:
            secret_key = session['mi_secret_key']
            session_token = session['mi_session_token']
            r = requests.post(f'https://{session["hostname"]}/api/auth/session/userkey', json={'appSecret': secret_key, 'token': session_token})
            if r.status_code != 200:
                return make_response(f'Failed to generate session: {r.text}', 500)
            j = r.json()

            access_token = j['accessToken']
            ccStr = f'{access_token}{secret_key}'
            token = hashlib.sha256(ccStr.encode('utf-8')).hexdigest()
            

        mi: Misskey = Misskey(address=session['hostname'], i=token)
        i = mi.i()

        session['username'] = i['username']
        session['acct'] = f'{i["username"]}@{session["hostname"]}'
        session['user_id'] = i['id']

        # 学習に使うノートを取得
        notes = []
        kwargs = {}
        for i in range(50):
            notes_block = mi.users_notes(session['user_id'], include_replies=False, include_my_renotes=False, limit=100, **kwargs)
            if not notes_block:
                break
            else:
                kwargs['until_id'] = notes_block[-1]['id']
                notes.extend(notes_block)

        # 解析用に文字列整形
        lines = []
        for note in notes:
            if note['text']:
                if len(note['text']) > 2:
                    for l in note['text'].splitlines():
                        lines.append(format_text(l))
        
        # MeCabで形態素解析
        parsed_text = []
        for line in lines:
            parsed_text.append(MeCab.Tagger('-Owakati').parse(line))
        
        # モデル作成
        try:
            text_model = markovify.NewlineText('\n'.join(parsed_text), state_size=2)
        except:
            return make_response('<meta name="viewport" content="width=device-width">モデル作成に失敗しました。学習に必要な投稿数が不足している可能性があります。', 500)

        # モデル保存
        try:
            cur = db.cursor()
            cur.execute('REPLACE INTO model_data(acct, data) VALUES (?, ?)', (session['acct'], text_model.to_json()))
            cur.close()
            db.commit()
        except:
            print(traceback.format_exc())
            return make_response('<meta name="viewport" content="width=device-width">データベースに書き込めませんでした。', 500)

        session['logged_in'] = True
        return redirect('/generate')
    
    if session['type'] == 'mastodon':
        
        auth_code = request.args.get('code')
        if not auth_code:
            return make_response('<meta name="viewport" content="width=device-width">認証に失敗しました。', 500)
        
        r = requests.post(f'https://{session["hostname"]}/oauth/token', json={
            'grant_type': 'client_credentials',
            'client_id': session['mstdn_app_key'],
            'client_secret': session['mstdn_app_secret'],
            'redirect_uris': session['mstdn_redirect_uri'],
            'code': auth_code
        })
        if r.status_code != 200:
            return make_response(f'Failed to get token: {r.text}', 500)
        
        d = r.json()
        token = d['access_token']

        r = requests.get('https://' + session['hostname'] + '/api/v1/apps/verify_credentials', headers={'Authorization': f'Bearer {token}'})
        if r.status_code != 200:
            return make_response(f'Failed to get verify credentials: {r.text}', 500)

        return '<meta name="viewport" content="width=device-width">現在Mastodonでも使えるように開発中です。<a href="/logout">ログアウト</a>'

        info = r.json()

        session['username'] = account_info['username']
        session['acct'] = f'{account_info["username"]}@{session["hostname"]}'

        mstdn = mastodon.Mastodon(client_id=session['mstdn_app_key'], client_secret=session['mstdn_app_secret'], access_token=token, api_base_url=f'https://{session["hostname"]}')
        
        toots = mstdn.account_statuses(account_info['id'], limit=1000)


        session['token'] = d['access_token']
        session['logged_in'] = True


@app.route('/generate')
def generate_page():
    return render_template('generate.html', text=None, acct='', share_text='')

@app.route('/generate/do', methods=['GET'])
def generate_do():
    query = request.args

    if not query.get('acct'):
        if not session.get('logged_in'):
            return '<meta name="viewport" content="width=device-width">自分の投稿から文章を作るにはログインしてください <a href="/#loginModal">ログインする</a>'
        
        # 自分のデータで作る
        acct = session['acct']
        if acct.startswith('@'):
            acct = acct[1:]

        cur = db.cursor()
        cur.execute('SELECT * FROM model_data WHERE acct = ?', (acct,))
        data = cur.fetchone()
        cur.close()

        if not data:
            return '<meta name="viewport" content="width=device-width">学習データが見つかりませんでした。 <a href="/logout">ログアウト</a>してから再度ログインしてください。'
    else:
        acct = query['acct']
        if acct.startswith('@'):
            acct = acct[1:]

        cur = db.cursor()
        cur.execute('SELECT * FROM model_data WHERE acct = ?', (acct,))
        data = cur.fetchone()
        cur.close()

        if not data:
            return f'<meta name="viewport" content="width=device-width">{acct} の学習データは見つかりませんでした。 '

    text_model = markovify.Text.from_json(data['data'])
    try:
        text = text_model.make_sentence(tries=100).replace(' ', '')
    except AttributeError:
        text = None
    if not text:
        return '<meta name="viewport" content="width=device-width">文章の生成に失敗しました <a href="javascript:location.reload();">再試行</a>'

    share_text = f'{text}\n\n{acct}\n#markov-generator-fedi\n{request.host_url}?no_ogp=true'
        
    return render_template('generate.html', text=text, acct=acct, share_text=urllib.parse.quote(share_text))

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


app.run(host='127.0.0.1', port=getattr(config, 'PORT') or 8888, debug=getattr(config, 'DEBUG') or True, threaded=True)