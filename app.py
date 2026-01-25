import json
from typing import Dict, Optional
import os
import jwt
from flask import Flask, render_template, request, redirect, session, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
import random
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!' # Used for Flask session security
socketio = SocketIO(app)

# SSO Configuration
SSO_LOGIN_URL = os.getenv('SSO_LOGIN_URL', 'https://chuvala.ru/login/google')
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

# Очередь ожидающих игроков
waiting_players = []
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

@app.route('/')
def index():
    user = get_current_user()
    return render_template('index.html', user=user)

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
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('index'))


# --- Socket.IO Events ---

@socketio.on('connect')
def handle_connect():
    # Validate session on connection
    # Note: Flask-SocketIO has access to the Flask session
    if not get_current_user():
        print(f'Unauthenticated client tried to connect: {request.sid}')
        # We can optionally disconnect them immediately, or just let them be "guests" 
        # but prevent game actions. For now, we allow connection but game logic checks auth.
        return 

    print(f'Client connected: {request.sid}, User: {session["user"]["email"]}')


@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')
    if request.sid in waiting_players:
        waiting_players.remove(request.sid)
    else:
        for room_id, game in active_games.items():
            if request.sid in game['players']:
                leave_room(room_id)
                opponent = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
                emit('opponent_disconnected', room=opponent)
                del active_games[room_id]
                break

@socketio.on('find_game')
def handle_find_game():
    if not get_current_user():
        emit('error', {'message': 'Authentication required'})
        return

    if request.sid in waiting_players:
        return

    waiting_players.append(request.sid)
    print(f'Player {request.sid} ({session["user"]["email"]}) is waiting for a game')

    if len(waiting_players) >= 2:
        player1 = waiting_players.pop(0)
        player2 = waiting_players.pop(0)

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


if __name__ == '__main__':
    socketio.run(app, debug=True)
