import json
from typing import Dict, Optional
import os
import jwt
from flask import Flask, request, redirect, session, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
import random
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!' # Used for Flask session security
socketio = SocketIO(app, cors_allowed_origins="*") # Allow CORS for devosh-style proxying

# SSO Configuration
SSO_LOGIN_URL = os.getenv('SSO_LOGIN_URL', 'http://localhost:8001/login')
JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'supersecretkeyformvpdev') # Shared with Chuvala
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM', 'HS256')

def get_words() -> Dict:
    with open("words.json", "r") as file:
        words = json.load(file)
        return words

# База данных слов: английское слово -> перевод
WORDS = get_words()


def generate_translations(word: str, num_options: int = 6) -> list[str]:
    """Generate a list of unique translation options containing exactly one correct answer."""
    correct_translation = WORDS[word]
    all_translations = list(WORDS.values())
    wrong_translations = [t for t in all_translations if t != correct_translation]
    if num_options - 1 > len(wrong_translations):
        num_wrong = max(1, min(len(wrong_translations), num_options - 1))
    else:
        num_wrong = num_options - 1
    options = random.sample(wrong_translations, num_wrong) + [correct_translation]
    random.shuffle(options)
    return options

# Очередь ожидающих игроков (Lobby): sid -> {email: ...}
waiting_players = {}
# Активные игры: room_id -> {'players': [player1, player2], 'word': word, ...}
active_games = {}

# --- Auth Helpers ---

def get_current_user() -> Optional[dict]:
    """Retrieve user info from Flask session."""
    return session.get('user')

def verify_token(token: str) -> Optional[dict]:
    """Verify JWT token from SSO provider."""
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload # Should contain 'sub' (email)
    except jwt.ExpiredSignatureError:
        print("Token expired")
        return None
    except jwt.InvalidTokenError:
        print("Invalid token")
        return None

# --- Routes ---

# @app.route('/') -> Served by Nginx Frontend

@app.route('/api/me')
def api_me():
    user = get_current_user()
    if user:
        return jsonify(user)
    return jsonify(None), 401

@app.route('/login')
def login():
    # Redirect to SSO provider with return URL
    # We want Chuvala to redirect back to /auth/callback here
    callback_url = url_for('auth_callback', _external=True)
    # Ensure HTTPS if behind proxy (controlled by env var)
    if os.getenv('FORCE_HTTPS', 'false').lower() == 'true':
        callback_url = callback_url.replace("http://", "https://")
        
    sso_url = f"{SSO_LOGIN_URL}?redirect_to={callback_url}"
    return redirect(sso_url)

@app.route('/auth/callback')
def auth_callback():
    token = request.args.get('token')
    if not token:
        return "Authentication failed: No token provided", 400
    
    payload = verify_token(token)
    if not payload:
        return "Authentication failed: Invalid token", 401
    
    # Store user in session
    session['user'] = {
        'email': payload.get('sub'),
        'token': token
    }
    # Redirect to root (frontend handled by Nginx)
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')

@app.route('/login/guest')
def login_guest():
    guest_id = random.randint(1000, 9999)
    session['user'] = {
        'email': f'Guest_{guest_id}',
        'token': 'guest'
    }
    return redirect('/')

# --- Helper: Broadcast Lobby State ---
def broadcast_lobby_state():
    """Send the current list of waiting players to everyone in the lobby."""
    players_list = [{'sid': sid, 'email': info['email']} for sid, info in waiting_players.items()]
    # Emit to all SIDs in the lobby
    for sid in waiting_players:
        emit('lobby_update', players_list, room=sid)

@socketio.on('connect')
def handle_connect(auth=None):
    # Validate session on connection
    if not get_current_user():
        print(f'Unauthenticated client tried to connect: {request.sid}')
        return 

    print(f'Client connected: {request.sid}, User: {session["user"]["email"]}')
    join_room(request.sid) # Explicitly join room with own SID
    
    # Broadcast debug to see if sockets work at all
    socketio.emit('debug_broadcast', {'msg': f'User {request.sid} connected'})


@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')
    if request.sid in waiting_players:
        del waiting_players[request.sid]
        broadcast_lobby_state()
    else:
        for room_id, game in active_games.items():
            if request.sid in game['players']:
                leave_room(room_id)
                opponent = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
                emit('opponent_disconnected', room=opponent)
                del active_games[room_id]
                break

@socketio.on('enter_lobby')
def handle_enter_lobby():
    user = get_current_user()
    if not user:
        emit('error', {'message': 'Authentication required'})
        return

    # Add to waiting list if not already there
    if request.sid not in waiting_players:
        waiting_players[request.sid] = {'email': user['email']}
        print(f'Player {request.sid} ({user["email"]}) entered lobby')
    
    # Send update to everyone (including self)
    broadcast_lobby_state()

@socketio.on('leave_lobby')
def handle_leave_lobby():
    if request.sid in waiting_players:
        del waiting_players[request.sid]
        broadcast_lobby_state()

@socketio.on('challenge_player')
def handle_challenge_player(data):
    target_sid = data.get('target_sid')
    challenger_sid = request.sid
    
    print(f"DEBUG: challenge_player called. Challenger: {challenger_sid}, Target: {target_sid}")
    print(f"DEBUG: Current waiting_players keys: {list(waiting_players.keys())}")
    
    if not target_sid or target_sid not in waiting_players:
        print(f"DEBUG: FAILURE - Target {target_sid} not found")
        emit('error', {'message': 'Player not found or no longer available'})
        return

    if target_sid == challenger_sid:
        print("DEBUG: FAILURE - Self challenge")
        return

    # Notify target player about the challenge
    challenger_info = waiting_players.get(challenger_sid)
    if challenger_info:
        print(f"DEBUG: SUCCESS - Broadcasting challenge_received to ALL (targeting {target_sid})")
        # WORKAROUND: Broadcast to all, client checks target_sid
        socketio.emit('challenge_received', {
            'target_sid': target_sid,
            'challenger_sid': challenger_sid,
            'challenger_email': challenger_info['email']
        }) 
    else:
        print(f"DEBUG: FAILURE - Challenger {challenger_sid} not found in waiting_players")
        emit('error', {'message': 'You are not in the lobby. Please refresh.'})


@socketio.on('decline_challenge')
def handle_decline_challenge(data):
    challenger_sid = data.get('challenger_sid')
    # Notify challenger
    emit('challenge_declined', {'message': 'Challenge declined'}, room=challenger_sid)


@socketio.on('accept_challenge')
def handle_accept_challenge(data):
    target_sid = request.sid
    challenger_sid = data.get('challenger_sid')

    # Verify both are still in lobby
    if target_sid not in waiting_players or challenger_sid not in waiting_players:
        emit('error', {'message': 'Cannot start game. One of the players left.'})
        return

    # Remove both from lobby
    player1 = challenger_sid
    player2 = target_sid
    
    del waiting_players[player1]
    del waiting_players[player2]
    
    broadcast_lobby_state()

    # Start Game
    room_id = f"room_{player1}_{player2}"
    join_room(room_id, player1)
    join_room(room_id, player2)

    word, translation = random.choice(list(WORDS.items()))
    translations = generate_translations(word, num_options=6)

    game_data = {
        'players': [player1, player2],
        'word': word,
        'translations': translations,
        'scores': {player1: 0, player2: 0},
        'answered': set(),
        'round_over': False
    }
    active_games[room_id] = game_data

    emit('game_start', {
        'word': word,
        'translations': translations,
        'opponent_connected': True
    }, room=player1)

    emit('game_start', {
        'word': word,
        'translations': translations,
        'opponent_connected': True
    }, room=player2)


@socketio.on('answer')
def handle_answer(data):
    room_id = None
    for r_id, game in active_games.items():
        if request.sid in game['players']:
            room_id = r_id
            break

    if not room_id:
        return

    game = active_games[room_id]
    
    # Initialize round/answer state if missing
    if 'answered' not in game: game['answered'] = set()
    if 'round_over' not in game: game['round_over'] = False

    if game.get('round_over'):
        return
        
    word = game['word']
    correct_translation = WORDS[word]

    if request.sid in game['answered']:
        return

    if data['answer'] == correct_translation:
        game['scores'][request.sid] += 1
        print(f"Player {request.sid} answered correctly. Score: {game['scores']}")
        
        # Check Win Condition
        if game['scores'][request.sid] >= 15:
            winner = request.sid
            loser = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
            
            emit('game_over', {
                'winner': True,
                'message': 'Поздравляем! Вы победили!',
                'final_scores': game['scores']
            }, room=winner)
            
            emit('game_over', {
                'winner': False,
                'message': 'Игра окончена. Вы проиграли.',
                'final_scores': game['scores']
            }, room=loser)
            
            del active_games[room_id]
            return

        game['round_over'] = True
        
        # Handle Round Win
        emit('answer_result', {
            'correct': True,
            'your_score': game['scores'][request.sid],
            'opponent_score': game['scores'][game['players'][0] if game['players'][1] == request.sid else game['players'][1]],
            'correct_answer': correct_translation,
            'you_answered': True
        }, room=request.sid)

        opponent = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
        emit('answer_result', {
            'correct': False,
            'your_score': game['scores'][opponent],
            'opponent_score': game['scores'][request.sid],
            'correct_answer': correct_translation,
            'you_answered': False
        }, room=opponent)

        # New Round
        time.sleep(2)
        word, translation = random.choice(list(WORDS.items()))
        translations = generate_translations(word, num_options=6)

        game['word'] = word
        game['translations'] = translations
        game['answered'] = set()
        game['round_over'] = False

        emit('new_round', {
            'word': word,
            'translations': translations
        }, room=room_id)
        
    else:
        # Wrong Answer
        game['answered'].add(request.sid)
        emit('answer_result', {
            'correct': False,
            'your_score': game['scores'][request.sid],
            'opponent_score': game['scores'][game['players'][0] if game['players'][1] == request.sid else game['players'][1]],
            'correct_answer': correct_translation
        }, room=request.sid)

        # If both answered wrong
        if len(game['answered']) >= 2 and not game.get('round_over'):
            game['round_over'] = True
            time.sleep(2)
            word, translation = random.choice(list(WORDS.items()))
            translations = generate_translations(word, num_options=6)

            game['word'] = word
            game['translations'] = translations
            game['answered'] = set()
            game['round_over'] = False

            emit('new_round', {
                'word': word,
                'translations': translations
            }, room=room_id)


@socketio.on('surrender')
def handle_surrender():
    # Find game
    room_id = None
    for r_id, game in active_games.items():
        if request.sid in game['players']:
            room_id = r_id
            break

    if not room_id:
        return

    game = active_games[room_id]
    loser = request.sid
    winner = game['players'][0] if game['players'][1] == loser else game['players'][1]

    emit('game_over', {
        'winner': True,
        'message': 'Соперник сдался! Вы победили!',
        'final_scores': game['scores']
    }, room=winner)

    emit('game_over', {
        'winner': False,
        'message': 'Вы сдались.',
        'final_scores': game['scores']
    }, room=loser)

    del active_games[room_id]


if __name__ == '__main__':
    socketio.run(app, debug=True)
