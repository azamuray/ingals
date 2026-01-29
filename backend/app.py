import eventlet
eventlet.monkey_patch()

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
WINNING_SCORE = int(os.getenv('WINNING_SCORE', 10))

# --- Bot Configuration ---
import threading

# Bot profiles - INVISIBLE to players (appear as normal users)
BOTS = [
    # Weak/Medium Bots (850-1300 ELO)
    {'name': 'Пепа', 'email': 'pepa_gamer@mail.ru', 'elo': 850, 'response_time': (4.0, 6.0), 'accuracy': 0.40},
    {'name': 'Дэвид_Бэкхан', 'email': 'david_beckhan@gmail.com', 'elo': 1000, 'response_time': (3.0, 5.0), 'accuracy': 0.50},
    {'name': 'Антор_ЧигурАм_амарян', 'email': 'anton_chigur@yandex.ru', 'elo': 1150, 'response_time': (2.5, 4.0), 'accuracy': 0.60},
    {'name': 'Бесшумно--летящий--воин', 'email': 'silent_warrior@mail.ru', 'elo': 1250, 'response_time': (2.0, 3.5), 'accuracy': 0.65},
    {'name': 'тройной_одеколон-Марк-Дакаскаса', 'email': 'triple_mark@gmail.com', 'elo': 1300, 'response_time': (1.8, 3.0), 'accuracy': 0.70},
    
    # Strong Bots (1600-1800 ELO)
    {'name': 'Джедай_Без_Меча', 'email': 'jedi_no_saber@mail.ru', 'elo': 1600, 'response_time': (1.2, 2.5), 'accuracy': 0.75},
    {'name': 'Pikachu-_ездит_на_жигули', 'email': 'pikachu_rides@gmail.com', 'elo': 1650, 'response_time': (1.0, 2.0), 'accuracy': 0.80},
    {'name': 'НеВыноси_Мусор', 'email': 'dont_take_trash@yandex.ru', 'elo': 1700, 'response_time': (0.8, 1.8), 'accuracy': 0.85},
    {'name': '_-Gandalf-_Sluшaet_Rap', 'email': 'gandalf_rap@mail.ru', 'elo': 1750, 'response_time': (0.7, 1.5), 'accuracy': 0.88},
    {'name': 'генадий___параходов', 'email': 'gennadiy_ships@gmail.com', 'elo': 1800, 'response_time': (0.6, 1.2), 'accuracy': 0.90},
]

# Track bot emails and configs for quick lookup
bot_emails = {bot['email'] for bot in BOTS}
bot_configs = {bot['email']: bot for bot in BOTS}
active_bot_threads = {}  # room_id -> thread

# --- Database Setup ---
import sqlite3
from flask import g
import os

# Ensure data directory exists
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE = os.path.join(DATA_DIR, 'users.db')

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                name TEXT,
                elo INTEGER DEFAULT 1200
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS friendships (
                user_email TEXT,
                friend_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_email, friend_email),
                FOREIGN KEY(user_email) REFERENCES users(email),
                FOREIGN KEY(friend_email) REFERENCES users(email)
            )

        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player1_email TEXT,
                player2_email TEXT,
                player1_score INTEGER,
                player2_score INTEGER,
                winner_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(player1_email) REFERENCES users(email),
                FOREIGN KEY(player2_email) REFERENCES users(email)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_words (
                user_email TEXT,
                word TEXT,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'learning', -- 'learning' or 'learned'
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_email, word),
                FOREIGN KEY(user_email) REFERENCES users(email)
            )
        ''')
        
        # Add/Update bots in database
        # Strategy: Insert if new, Update name if exists (but PRESERVE ELO)
        for bot in BOTS:
            # 1. Try to insert new bot with default config
            cursor.execute('INSERT OR IGNORE INTO users (email, name, elo) VALUES (?, ?, ?)',
                         (bot['email'], bot['name'], bot['elo']))
            
            # 2. Update name in case it changed in config (but don't touch ELO)
            cursor.execute('UPDATE users SET name = ? WHERE email = ?', (bot['name'], bot['email']))
            
            print(f"Added/Updated bot: {bot['name']}")

        # Test Data Injection REMOVED for production
        
        db.commit()

init_db()  # Initialize on startup

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
        # Fetch detailed profile from DB
        db = get_db()
        row = db.execute('SELECT name, elo FROM users WHERE email = ?', (user['email'],)).fetchone()
        
        # Fetch friends
        # Fetch friends with details (Name, ELO)
        friends_rows = db.execute('''
            SELECT u.email, u.name, u.elo 
            FROM friendships f
            JOIN users u ON f.friend_email = u.email
            WHERE f.user_email = ?
        ''', (user['email'],)).fetchall()
        
        friends = []
        for f in friends_rows:
            friends.append({
                'email': f['email'],
                'name': f['name'] or f['email'],
                'elo': f['elo']
            })

        user_data = {
            'email': user['email'],
            'name': row['name'] if row else None,
            'elo': row['elo'] if row else 1200,
            'friends': friends
        }
        
        # If user not in DB, create them
        if not row:
            db.execute('INSERT INTO users (email, name, elo) VALUES (?, ?, ?)', (user['email'], None, 1200))
            db.commit()
            
        return jsonify(user_data)
    return jsonify(None), 401

@app.route('/api/friends/add', methods=['POST'])
def add_friend():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
        
    data = request.json
    friend_email = data.get('friend_email')
    
    if not friend_email:
        return jsonify({'error': 'Missing friend_email'}), 400
        
    if friend_email == user['email']:
        return jsonify({'error': 'Cannot add yourself'}), 400

    db = get_db()
    try:
        db.execute('INSERT OR IGNORE INTO friendships (user_email, friend_email) VALUES (?, ?)', 
                  (user['email'], friend_email))
        db.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        print(f"Error adding friend: {e}")
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/friends/remove', methods=['POST'])
def remove_friend():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
        
    data = request.json
    friend_email = data.get('friend_email')
    
    if not friend_email:
        return jsonify({'error': 'Missing friend_email'}), 400

    db = get_db()
    try:
        db.execute('DELETE FROM friendships WHERE user_email = ? AND friend_email = ?', 
                  (user['email'], friend_email))
        db.commit()
        return jsonify({'status': 'ok'})
    except Exception as e:
        print(f"Error removing friend: {e}")
        return jsonify({'error': 'Database error'}), 500

@app.route('/api/profile/<identifier>')
def get_public_profile(identifier):
    current = get_current_user()
    if not current:
        return jsonify({'error': 'Not authenticated'}), 401
    
    target_email = current['email'] if identifier == 'me' else identifier
    
    db = get_db()
    
    # User Info
    user_row = db.execute('SELECT name, elo FROM users WHERE email = ?', (target_email,)).fetchone()
    if not user_row:
        # Check if it's a bot
        bot_config = next((b for b in BOTS if b['email'] == target_email), None)
        if bot_config:
             return jsonify({
                'email': target_email,
                'name': bot_config['name'],
                'elo': bot_config['elo'], # Static ELO for profile? Or dynamic from DB? Use DB if available.
                'stats': {'total_games': 999, 'wins': 500, 'losses': 499}, # Fake stats for bots
                'is_friend': True, # Bots are friends
                'is_me': False
             })
        return jsonify({'error': 'User not found'}), 404
        
    # Stats
    games_rows = db.execute('''
        SELECT count(*) as total, 
        sum(case when winner_email = ? then 1 else 0 end) as wins 
        FROM games 
        WHERE player1_email = ? OR player2_email = ?
    ''', (target_email, target_email, target_email)).fetchone()
    
    total_games = games_rows['total']
    wins = games_rows['wins'] or 0
    losses = total_games - wins
    
    # Is Friend?
    is_friend = False
    if current['email'] != target_email:
        friend_row = db.execute('SELECT 1 FROM friendships WHERE user_email = ? AND friend_email = ?', 
                               (current['email'], target_email)).fetchone()
        is_friend = bool(friend_row)
        
    # Fetch History (Last 20 games) for EVERYONE
    history_rows = db.execute('''
        SELECT id, player1_email, player2_email, player1_score, player2_score, winner_email, created_at 
        FROM games 
        WHERE player1_email = ? OR player2_email = ? 
        ORDER BY created_at DESC LIMIT 20
    ''', (target_email, target_email)).fetchall()
    
    history = []
    for row in history_rows:
        # Determine opponent
        is_p1 = row['player1_email'] == target_email
        opponent_email = row['player2_email'] if is_p1 else row['player1_email']
        
        # Get opponent name
        opp_row = db.execute('SELECT name FROM users WHERE email = ?', (opponent_email,)).fetchone()
        opponent_name = opp_row['name'] if opp_row else opponent_email
        
        history.append({
            'opponent_name': opponent_name,
            'opponent_email': opponent_email,
            'my_score': row['player1_score'] if is_p1 else row['player2_score'],
            'opponent_score': row['player2_score'] if is_p1 else row['player1_score'],
            'won': row['winner_email'] == target_email,
            'date': row['created_at']
        })

    return jsonify({
        'email': target_email,
        'name': user_row['name'] or target_email,
        'elo': user_row['elo'],
        'stats': {
            'total_games': total_games,
            'wins': wins,
            'losses': losses
        },
        'history': history,
        'is_friend': is_friend,
        'is_me': current['email'] == target_email
    })

@app.route('/api/me/stats')
def get_my_stats():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Not authenticated'}), 401
        
    db = get_db()
    
    # Words
    words_rows = db.execute('SELECT word, correct_count, wrong_count, status, last_seen FROM user_words WHERE user_email = ? ORDER BY last_seen DESC', (user['email'],)).fetchall()
    words = [dict(row) for row in words_rows]
    
    # History (Last 20 games)
    history_rows = db.execute('''
        SELECT id, player1_email, player2_email, player1_score, player2_score, winner_email, created_at 
        FROM games 
        WHERE player1_email = ? OR player2_email = ? 
        ORDER BY created_at DESC LIMIT 20
    ''', (user['email'], user['email'])).fetchall()
    
    history = []
    for row in history_rows:
        # Determine opponent
        is_p1 = row['player1_email'] == user['email']
        opponent_email = row['player2_email'] if is_p1 else row['player1_email']
        
        # Get opponent name
        opp_row = db.execute('SELECT name FROM users WHERE email = ?', (opponent_email,)).fetchone()
        opponent_name = opp_row['name'] if opp_row else opponent_email
        
        history.append({
            'opponent_name': opponent_name,
            'opponent_email': opponent_email,
            'my_score': row['player1_score'] if is_p1 else row['player2_score'],
            'opponent_score': row['player2_score'] if is_p1 else row['player1_score'],
            'won': row['winner_email'] == user['email'],
            'date': row['created_at']
        })
        
    return jsonify({
        'words': words,
        'history': history
    })

@app.route('/api/profile', methods=['POST'])
def update_profile():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json
    name = data.get('name')
    
    if not name or len(name) < 2:
        return jsonify({'error': 'Invalid name'}), 400
        
    db = get_db()
    db.execute('UPDATE users SET name = ? WHERE email = ?', (name, user['email']))
    db.commit()
    
    return jsonify({'success': True})

@app.route('/api/me/words/toggle', methods=['POST'])
def toggle_word_status():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    word = data.get('word')
    
    if not word:
        return jsonify({'error': 'Missing word'}), 400
        
    db = get_db()
    # Check current status
    row = db.execute('SELECT status FROM user_words WHERE user_email = ? AND word = ?', (user['email'], word)).fetchone()
    
    new_status = 'learned'
    if row:
        current_status = row['status']
        new_status = 'learning' if current_status == 'learned' else 'learned'
        
        db.execute('UPDATE user_words SET status = ? WHERE user_email = ? AND word = ?', (new_status, user['email'], word))
    else:
        # If word doesn't exist yet, insert as learned (since they clicked to toggle it presumably from somewhere, or maybe default to learning?)
        # Actually user can only click words they have seen. If not seen, maybe insert as learning.
        # But for now assume existing words. If not exists, insert as Learned.
        db.execute('''
            INSERT INTO user_words (user_email, word, correct_count, wrong_count, status, last_seen)
            VALUES (?, ?, 0, 0, ?, CURRENT_TIMESTAMP)
        ''', (user['email'], word, new_status))
        
    db.commit()
    return jsonify({'status': new_status})

@app.route('/api/leaderboard')
def get_leaderboard():
    db = get_db()
    current_user = get_current_user()
    
    # Get Top 30
    top_players = db.execute('''
        SELECT name, email, elo 
        FROM users 
        WHERE (elo != 1200 OR name IS NOT NULL)
          AND email NOT LIKE 'Guest_%'
        ORDER BY elo DESC 
        LIMIT 30
    ''').fetchall()
    
    result = []
    for player in top_players:
        result.append({
            'name': player['name'] or player['email'],
            'elo': player['elo'],
            'email': player['email']
        })
        
    # Check if current user is in top 30
    is_in_top = False
    if current_user:
        for p in result:
            if p['email'] == current_user['email']:
                is_in_top = True
                break
                
        # If not in top, append user with their rank
        if not is_in_top:
            user_row = db.execute('SELECT name, elo FROM users WHERE email = ?', (current_user['email'],)).fetchone()
            if user_row:
                my_elo = user_row['elo']
                # Calculate Rank: count users with ELO > my_elo + 1
                rank_row = db.execute('SELECT count(*) FROM users WHERE elo > ? AND email NOT LIKE \'Guest_%\'', (my_elo,)).fetchone()
                rank = rank_row[0] + 1
                
                result.append({
                     'name': user_row['name'] or current_user['email'],
                     'elo': my_elo,
                     'email': current_user['email'],
                     'rank': rank,
                     'is_me_outside': True
                })

    return jsonify(result)

@app.route('/login')
def login():
    # Check if currently logged in as Guest to merge data
    current_user = get_current_user()
    if current_user and current_user['email'].startswith('Guest_'):
        session['merge_guest_email'] = current_user['email']
        print(f"Stashing guest session for merge: {current_user['email']}")
        
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
    new_email = payload.get('sub')
    session['user'] = {
        'email': new_email,
        'token': token
    }
    
    # --- MIGRATION LOGIC ---
    merge_guest_email = session.pop('merge_guest_email', None)
    if merge_guest_email:
        print(f"Merging guest {merge_guest_email} into {new_email}")
        db = get_db()
        
        # Get Guest Data
        guest_row = db.execute('SELECT elo FROM users WHERE email = ?', (merge_guest_email,)).fetchone()
        
        if guest_row:
            guest_elo = guest_row['elo']
            
            # Ensure new user exists
            user_row = db.execute('SELECT elo FROM users WHERE email = ?', (new_email,)).fetchone()
            if not user_row:
                db.execute('INSERT INTO users (email, name, elo) VALUES (?, ?, ?)', (new_email, None, guest_elo))
            else:
                # If new user already has default ELO (1200) or we blindly prefer Guest progress (User choice implies intent to save)
                # Let's take the MAX elo to be safe, or just overwrite if user_elo is 1200 (fresh).
                # Strategy: Overwrite if current user is 'fresh' (1200 or no games). 
                # For simplicity: Always overwrite with Guest ELO if Guest ELO != 1200.
                if guest_elo != 1200:
                     db.execute('UPDATE users SET elo = ? WHERE email = ?', (guest_elo, new_email))
            
            # Delete Guest account after successful migration
            db.execute('DELETE FROM users WHERE email = ?', (merge_guest_email,))
            print(f"Deleted guest account: {merge_guest_email}")
            
            db.commit()
            
    # Redirect to root (frontend handled by Nginx)
    return redirect('/')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/')

@app.route('/login/guest')
def login_guest():
    guest_id = random.randint(1000, 9999)
    email = f'Guest_{guest_id}'
    session['user'] = {
        'email': email,
        'token': 'guest'
    }
    
    # Ensure guest exists in DB
    db = get_db()
    # Check if exists
    row = db.execute('SELECT 1 FROM users WHERE email = ?', (email,)).fetchone()
    if not row:
        db.execute('INSERT INTO users (email, name, elo) VALUES (?, ?, ?)', (email, f"Guest {guest_id}", 1200))
        db.commit()
        
    return redirect('/')

# --- ELO Calculation Helper ---
def calculate_elo(winner_elo, loser_elo, k_factor=32):
    """
    Calculate new ELO ratings using standard formula.
    Ra' = Ra + K * (Sa - Ea)
    """
    expected_winner = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_loser = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))
    
    new_winner_elo = round(winner_elo + k_factor * (1 - expected_winner))
    new_loser_elo = round(loser_elo + k_factor * (0 - expected_loser))
    
    return new_winner_elo, new_loser_elo

# --- Helper: Broadcast Lobby State ---
def broadcast_lobby_state():
    # Only active users
    active_users = []
    db = get_db()
    
    for sid, user in waiting_players.items():
        # Fetch latest ELO and Name from DB
        row = db.execute('SELECT name, elo FROM users WHERE email = ?', (user['email'],)).fetchone()
        name = row['name'] if row and row['name'] else user['email']
        elo = row['elo'] if row else 1200
        
        active_users.append({
            'sid': sid, 
            'email': user['email'], # Keep email for unique ID internally if needed
            'name': name,
            'elo': elo,
            'is_bot': sid.startswith('bot_') or user['email'] in bot_emails
        })
        
    online_count = len(waiting_players)
    
    # Sort active_users for optimization if needed, but client handles it too.
    
    socketio.emit('lobby_update', {
        'players': active_users,
        'online_count': online_count
    }, namespace='/')

@socketio.on('connect')
def handle_connect(auth=None):
    # Validate session on connection
    if not get_current_user():
        print(f'Unauthenticated client tried to connect: {request.sid}')
        return 

    print(f'Client connected: {request.sid}, User: {session["user"]["email"]}')
    socketio.server.enter_room(request.sid, request.sid, namespace='/') # Explicitly join room with own SID
    
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
    
    # Ensure bots are in lobby (add if missing)
    for bot in BOTS:
        bot_sid = f"bot_{bot['email']}"
        if bot_sid not in waiting_players:
            waiting_players[bot_sid] = {'email': bot['email']}
    
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
    rounds = data.get('rounds', WINNING_SCORE)  # Default from config
    
    # Validate rounds (5-30)
    if not isinstance(rounds, int) or rounds < 5 or rounds > 30:
        rounds = WINNING_SCORE
    
    print(f"DEBUG: challenge_player called. Challenger: {challenger_sid}, Target: {target_sid}, Rounds: {rounds}")
    print(f"DEBUG: Current waiting_players keys: {list(waiting_players.keys())}")
    
    if not target_sid or target_sid not in waiting_players:
        print(f"DEBUG: FAILURE - Target {target_sid} not found")
        emit('error', {'message': 'Player not found or no longer available'})
        return

    if target_sid == challenger_sid:
        print("DEBUG: FAILURE - Self challenge")
        return

    # Check if target is a bot
    target_email = waiting_players.get(target_sid, {}).get('email')
    bot_emails = [bot['email'] for bot in BOTS] # Assuming BOTS is defined globally
    if target_email in bot_emails:
        # Bot auto-accepts after brief delay
        def bot_auto_accept_job():
            socketio.sleep(random.uniform(0.3, 0.8))  # Human-like delay
            with app.app_context():
                start_game_for_bot(challenger_sid, target_sid, rounds)
        
        socketio.start_background_task(bot_auto_accept_job)
        print(f"Bot {target_email} will auto-accept challenge")
        return
    
    # Human player - send challenge notification
    challenger_info = waiting_players.get(challenger_sid)
    if challenger_info:
        print(f"DEBUG: SUCCESS - Broadcasting challenge_received to ALL (targeting {target_sid})")
        # WORKAROUND: Broadcast to all, client checks target_sid
        socketio.emit('challenge_received', {
            'target_sid': target_sid,
            'challenger_sid': challenger_sid,
            'challenger_email': challenger_info['email'],
            'rounds': rounds
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
    rounds = data.get('rounds', WINNING_SCORE)  # Get rounds from challenge
    
    # Validate rounds again
    if not isinstance(rounds, int) or rounds < 5 or rounds > 30:
        rounds = WINNING_SCORE

    # Verify both are still in lobby
    if target_sid not in waiting_players or challenger_sid not in waiting_players:
        emit('error', {'message': 'Cannot start game. One of the players left.'})
        return

    # Remove both from lobby
    player1 = challenger_sid
    player2 = target_sid
    
    # Capture emails before removing
    email1 = waiting_players[player1]['email']
    email2 = waiting_players[player2]['email']
    
    del waiting_players[player1]
    del waiting_players[player2]
    
    broadcast_lobby_state()

    # Start Game
    room_id = f"room_{player1}_{player2}"
    socketio.server.enter_room(player1, room_id, namespace='/')
    socketio.server.enter_room(player2, room_id, namespace='/')

    word, translation = random.choice(list(WORDS.items()))
    translations = generate_translations(word, num_options=6)

    game_data = {
        'players': [player1, player2],
        'emails': {player1: email1, player2: email2},
        'word': word,
        'translations': translations,
        'scores': {player1: 0, player2: 0},
        'answered': set(),
        'round_over': False,
        'winning_score': rounds  # Store game-specific winning score
    }

    active_games[room_id] = game_data

    # Fetch names from DB
    db = get_db()
    name1 = db.execute('SELECT name FROM users WHERE email = ?', (email1,)).fetchone()['name'] or email1
    name2 = db.execute('SELECT name FROM users WHERE email = ?', (email2,)).fetchone()['name'] or email2

    socketio.emit('game_start', {
        'word': word,
        'translations': translations,
        'opponent_connected': True,
        'winning_score': rounds,
        'opponent_name': name2,
        'opponent_email': email2
    }, room=player1, namespace='/')

    socketio.emit('game_start', {
        'word': word,
        'translations': translations,
        'opponent_connected': True,
        'winning_score': rounds,
        'opponent_name': name1,
        'opponent_email': email1
    }, room=player2, namespace='/')
    
    # Check if any player is a bot and start bot playing thread
    player1_email = email1
    player2_email = email2
    bot_sid = None
    
    if player1_email in bot_emails:
        bot_sid = player1
    elif player2_email in bot_emails:
        bot_sid = player2
    
    if bot_sid:
        # Start bot playing thread
        socketio.start_background_task(bot_play_game, room_id, bot_sid)
        print(f"Started bot game task for room {room_id}")


def start_game_for_bot(challenger_sid, bot_sid, rounds):
    """Start a game when bot auto-accepts challenge."""
    with app.app_context():
        # Check both are still in lobby
        if challenger_sid not in waiting_players or bot_sid not in waiting_players:
            return
        
        player1 = challenger_sid
        player2 = bot_sid
        
        # Check if game already exists to prevent double threads
        room_id = f"room_{player1}_{player2}"
        if room_id in active_games:
            print(f"DEBUG: Game {room_id} already exists. Skipping duplicate start.")
            return

        email1 = waiting_players[player1]['email']
        email2 = waiting_players[player2]['email']
        
        del waiting_players[player1]
        del waiting_players[player2]
        
        broadcast_lobby_state()
        
        # Start Game
        socketio.server.enter_room(player1, room_id, namespace='/')
        # Skip enter_room for bot_sid as it's not a real socket connection

        
        word, translation = random.choice(list(WORDS.items()))
        translations = generate_translations(word, num_options=6)
        
        game_data = {
            'players': [player1, player2],
            'emails': {player1: email1, player2: email2},
            'word': word,
            'translations': translations,
            'scores': {player1: 0, player2: 0},
            'answered': set(),
            'round_over': False,
            'winning_score': rounds
        }
        
        active_games[room_id] = game_data
        
        # Fetch names from DB
        db = get_db()
        name1 = db.execute('SELECT name FROM users WHERE email = ?', (email1,)).fetchone()['name'] or email1
        name2 = db.execute('SELECT name FROM users WHERE email = ?', (email2,)).fetchone()['name'] or email2
        
        socketio.emit('game_start', {
            'word': word,
            'translations': translations,
            'opponent_connected': True,
            'winning_score': rounds,
            'opponent_name': name2,
            'opponent_email': email2
        }, room=player1, namespace='/')
        
        socketio.emit('game_start', {
            'word': word,
            'translations': translations,
            'opponent_connected': True,
            'winning_score': rounds,
            'opponent_name': name1,
            'opponent_email': email1
        }, room=player2, namespace='/')
        
        # Start bot playing thread
        socketio.start_background_task(bot_play_game, room_id, bot_sid)



import threading

# Global set to track active bot threads
active_bot_threads = set()
bot_thread_lock = threading.Lock()

def bot_play_game(room_id, bot_sid):
    """Bot plays the game automatically with delays and accuracy based on config."""
    thread_id = f"{room_id}_{bot_sid}"
    
    with bot_thread_lock:
        if thread_id in active_bot_threads:
            print(f"DEBUG: Duplicate bot thread prevented for {thread_id}")
            return
        active_bot_threads.add(thread_id)
        
    print(f"DEBUG: Starting bot thread {thread_id}. Active threads: {len(active_bot_threads)}")

    try:
        bot_email = waiting_players.get(bot_sid, {}).get('email') if bot_sid in waiting_players else None
        
        # If bot was removed from waiting_players, get from game emails
        if not bot_email and room_id in active_games:
            game = active_games[room_id]
            bot_email = game['emails'].get(bot_sid)
        
        if not bot_email or bot_email not in bot_configs:
            print(f"Bot config not found for {bot_sid}")
            return
        
        bot_config = bot_configs[bot_email]
        print(f"DEBUG: Bot {bot_config['name']} started playing in {room_id}")
        
        while room_id in active_games:
            socketio.sleep(0.1)  # Check frequently
            
            game = active_games.get(room_id)

            if not game or game.get('round_over'):
                continue
            
            # Check if bot needs to answer
            if bot_sid in game['answered']:
                continue
            
            # Capture current word to ensure we answer the same round later
            current_word = game['word']

            # Wait for response time
            min_time, max_time = bot_config['response_time']
            delay = random.uniform(min_time, max_time)
            print(f"DEBUG: Bot {bot_config['name']} sleeping for {delay:.2f}s in {room_id}")
            socketio.sleep(delay)
            print(f"DEBUG: Bot {bot_config['name']} woke up in {room_id}")
            
            # Double-check game still exists
            if room_id not in active_games:
                break
            
            game = active_games[room_id]
            if bot_sid in game['answered'] or game.get('round_over'):
                continue
                
            # Ensure we are still in the same round
            if game['word'] != current_word:
                continue
            
            # Determine answer based on accuracy
            word = game['word']
            correct_answer = WORDS[word]
            translations = game['translations']
            
            if random.random() < bot_config['accuracy']:
                answer = correct_answer
            else:
                wrong = [t for t in translations if t != correct_answer]
                answer = random.choice(wrong) if wrong else correct_answer
            
            # Submit answer - mark as answered and process
            game['answered'].add(bot_sid)
            
            # Process the answer (similar to on_answer logic)
            if answer == correct_answer:
                game['scores'][bot_sid] += 1
                print(f"DEBUG: Bot {bot_config['name']} answering CORRECTLY in {room_id}. Score: {game['scores']}")
                
                # Send results IMMEDIATELY so frontend updates score BEFORE game over
                opponent = game['players'][0] if game['players'][1] == bot_sid else game['players'][1]
                
                socketio.emit('answer_result', {
                    'correct': True,
                    'your_score': game['scores'][bot_sid],
                    'opponent_score': game['scores'][opponent],
                    'correct_answer': correct_answer,
                    'you_answered': True
                }, room=bot_sid, namespace='/')
                
                socketio.emit('answer_result', {
                    'correct': True,
                    'your_score': game['scores'][opponent],
                    'opponent_score': game['scores'][bot_sid],
                    'correct_answer': correct_answer,
                    'you_answered': False
                }, room=opponent, namespace='/')
                
                # Check Win Condition
                winning_score = game.get('winning_score', WINNING_SCORE)
                if game['scores'][bot_sid] >= winning_score:
                    print(f"DEBUG: Bot WON. Triggering Game Over.")
                    # Give frontend a moment to process the score update
                    socketio.sleep(0.5)
                    
                    # Bot won - trigger game over
                    winner_sid = bot_sid
                    loser_sid = game['players'][0] if game['players'][1] == bot_sid else game['players'][1]
                    
                    # ELO update
                    with app.app_context():
                        db = get_db()
                        winner_email = game['emails'].get(winner_sid)
                        loser_email = game['emails'].get(loser_sid)
                        
                        winner_row = db.execute('SELECT elo FROM users WHERE email = ?', (winner_email,)).fetchone()
                        loser_row = db.execute('SELECT elo FROM users WHERE email = ?', (loser_email,)).fetchone()
                        
                        winner_elo = winner_row['elo'] if winner_row else 1200
                        loser_elo = loser_row['elo'] if loser_row else 1200
                        
                        new_winner_elo, new_loser_elo = calculate_elo(winner_elo, loser_elo)
                        
                        db.execute('UPDATE users SET elo = ? WHERE email = ?', (new_winner_elo, winner_email))
                        db.execute('UPDATE users SET elo = ? WHERE email = ?', (new_loser_elo, loser_email))
                        
                        # Log Game
                        db.execute('''
                            INSERT INTO games (player1_email, player2_email, player1_score, player2_score, winner_email)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (winner_email, loser_email, game['scores'][winner_sid], game['scores'][loser_sid], winner_email))
                        
                        db.commit()
                        
                        # Fetch names for messages
                        winner_name = db.execute('SELECT name FROM users WHERE email = ?', (winner_email,)).fetchone()['name'] or winner_email
                        loser_name = db.execute('SELECT name FROM users WHERE email = ?', (loser_email,)).fetchone()['name'] or loser_email

                        socketio.emit('game_over', {
                            'winner': True,
                            'message': f'Поздравляем! Вы победили: {loser_name} 🏆',
                            'final_scores': game['scores'],
                            'elo_update': {'old': winner_elo, 'new': new_winner_elo}
                        }, room=winner_sid, namespace='/')
                        
                        socketio.emit('game_over', {
                            'winner': False,
                            'message': f'Игра окончена. Победил {winner_name} 😔',
                            'final_scores': game['scores'],
                            'elo_update': {'old': loser_elo, 'new': new_loser_elo}
                        }, room=loser_sid, namespace='/')
                        
                        del active_games[room_id]
                        return
                
                game['round_over'] = True
                
                # New round after delay
                socketio.sleep(2)
                if room_id not in active_games:
                    break
                    
                word, translation = random.choice(list(WORDS.items()))
                translations = generate_translations(word, num_options=6)
                
                game['word'] = word
                game['translations'] = translations
                game['answered'] = set()
                game['round_over'] = False
                
                print(f"DEBUG: Bot {bot_config['name']} starting new round after correct answer in {room_id}")
                # Emit to room AND explicitly to opponent to be safe
                socketio.emit('new_round', {
                    'word': word,
                    'translations': translations
                }, room=room_id, namespace='/')
                
                socketio.emit('new_round', {
                    'word': word,
                    'translations': translations
                }, room=opponent, namespace='/')
                
                print(f"DEBUG: Bot {bot_config['name']} submitted correct answer. Emitting new_round explicitly.")
                
            else:
                print(f"DEBUG: Bot {bot_config['name']} answering INCORRECTLY in {room_id}")
                print(f"DEBUG: Bot {bot_config['name']} answered INCORRECTLY. Score UNCHANGED: {game['scores']}")
                # Wrong answer - match on_answer structure
                opponent = game['players'][0] if game['players'][1] == bot_sid else game['players'][1]
                
                socketio.emit('answer_result', {
                    'correct': False,
                    'your_score': game['scores'][bot_sid],
                    'opponent_score': game['scores'][opponent],
                    'correct_answer': correct_answer,
                    'you_answered': True
                }, room=bot_sid, namespace='/')
                
                socketio.emit('answer_result', {
                    'correct': False,
                    'your_score': game['scores'][opponent],
                    'opponent_score': game['scores'][bot_sid],
                    'correct_answer': correct_answer,
                    'you_answered': False
                }, room=opponent, namespace='/')

                # If both answered wrong
                if len(game['answered']) >= 2 and not game.get('round_over'):
                    game['round_over'] = True
                    socketio.sleep(2)
                    if room_id not in active_games:
                        return
                        
                    word, translation = random.choice(list(WORDS.items()))
                    translations = generate_translations(word, num_options=6)
                    
                    game['word'] = word
                    game['translations'] = translations
                    game['answered'] = set()
                    game['round_over'] = False
                    
                    print(f"DEBUG: Bot processing BOTH WRONG in {room_id}")
                    socketio.emit('new_round', {
                        'word': word,
                        'translations': translations
                    }, room=room_id, namespace='/')
                    
                    socketio.emit('new_round', {
                        'word': word,
                        'translations': translations
                    }, room=opponent, namespace='/')
                    print(f"DEBUG: Bot {bot_config['name']} finished emitting new_round signals.")

    except Exception as e:
        print(f"CRITICAL ERROR in bot_play_game for {room_id}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        active_bot_threads.discard(thread_id)
        print(f"DEBUG: Bot thread finished for {thread_id}. Remaining threads: {len(active_bot_threads)}")




@socketio.on('answer')
def on_answer(data):
    print(f"DEBUG: on_answer received from {request.sid}: {data}")
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
        print(f"DEBUG: Player {request.sid} answered CORRECTLY. Score: {game['scores']}")
        
        # Emit answer_result IMMEDIATELY
        opponent = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
        
        socketio.emit('answer_result', {
            'correct': True,
            'your_score': game['scores'][request.sid],
            'opponent_score': game['scores'][opponent],
            'correct_answer': correct_translation,
            'you_answered': True
        }, room=request.sid, namespace='/')

        socketio.emit('answer_result', {
            'correct': True,
            'your_score': game['scores'][opponent],
            'opponent_score': game['scores'][request.sid],
            'correct_answer': correct_translation,
            'you_answered': False
        }, room=opponent, namespace='/')
        
        # Log Word Stats (Correct)
        with app.app_context():
            db = get_db()
            answering_email = game['emails'].get(request.sid)
            if answering_email:
                 db.execute('''
                    INSERT INTO user_words (user_email, word, correct_count, wrong_count, status, last_seen)
                    VALUES (?, ?, 1, 0, 'learned', CURRENT_TIMESTAMP)
                    ON CONFLICT(user_email, word) DO UPDATE SET
                    correct_count = correct_count + 1,
                    status = CASE WHEN (correct_count + 1) > (wrong_count * 2) THEN 'learned' ELSE status END, -- Auto-promote if doing well
                    last_seen = CURRENT_TIMESTAMP
                ''', (answering_email, word))
                 db.commit()
        
        # Check Win Condition
        # Check Win Condition - use game-specific winning score
        winning_score = game.get('winning_score', WINNING_SCORE)
        print(f"DEBUG: Win Check (Player) - Score: {game['scores'][request.sid]}, Target: {winning_score}")
        
        if game['scores'][request.sid] >= winning_score:
            print(f"DEBUG: Player WON. Triggering Game Over.")
            # Give frontend a moment
            socketio.sleep(0.5)
            
            winner_sid = request.sid
            loser_sid = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
            
            # --- ELO UPDATE ---
            with app.app_context():
                db = get_db()
                
                winner_email = game['emails'].get(winner_sid)
                loser_email = game['emails'].get(loser_sid)
                
                winner_row = db.execute('SELECT elo FROM users WHERE email = ?', (winner_email,)).fetchone()
                loser_row = db.execute('SELECT elo FROM users WHERE email = ?', (loser_email,)).fetchone()
                
                winner_elo = winner_row['elo'] if winner_row else 1200
                loser_elo = loser_row['elo'] if loser_row else 1200
                
                new_winner_elo, new_loser_elo = calculate_elo(winner_elo, loser_elo)
                
                # Update DB
                db.execute('UPDATE users SET elo = ? WHERE email = ?', (new_winner_elo, winner_email))
                db.execute('UPDATE users SET elo = ? WHERE email = ?', (new_loser_elo, loser_email))
                db.commit()
                
                
                # Fetch names for messages
                winner_name = db.execute('SELECT name FROM users WHERE email = ?', (winner_email,)).fetchone()['name'] or winner_email
                loser_name = db.execute('SELECT name FROM users WHERE email = ?', (loser_email,)).fetchone()['name'] or loser_email

                # Log Game
                db.execute('''
                    INSERT INTO games (player1_email, player2_email, player1_score, player2_score, winner_email)
                    VALUES (?, ?, ?, ?, ?)
                ''', (winner_email, loser_email, game['scores'][winner_sid], game['scores'][loser_sid], winner_email))
                db.commit()

                socketio.emit('game_over', {
                    'winner': True,
                    'message': f'Поздравляем! Вы победили: {loser_name} 🏆',
                    'final_scores': game['scores'],
                    'elo_update': {'old': winner_elo, 'new': new_winner_elo}
                }, room=winner_sid, namespace='/')
                
                socketio.emit('game_over', {
                    'winner': False,
                    'message': f'Игра окончена. Победил {winner_name} 😔',
                    'final_scores': game['scores'],
                    'elo_update': {'old': loser_elo, 'new': new_loser_elo}
                }, room=loser_sid, namespace='/')
                
                del active_games[room_id]
                return
            
        game['round_over'] = True
        
        # New Round
        socketio.sleep(2)
        if room_id not in active_games:
            return
            
        word, translation = random.choice(list(WORDS.items()))
        translations = generate_translations(word, num_options=6)

        game['word'] = word
        game['translations'] = translations
        game['answered'] = set()
        game['round_over'] = False

        print(f"DEBUG: Player {request.sid} correct - emitting new round in {room_id}")
        socketio.emit('new_round', {
            'word': word,
            'translations': translations
        }, room=room_id, namespace='/')
        
    else:
        # Wrong Answer
        game['answered'].add(request.sid)
        opponent = game['players'][0] if game['players'][1] == request.sid else game['players'][1]
        
        socketio.emit('answer_result', {
            'correct': False,
            'your_score': game['scores'][request.sid],
            'opponent_score': game['scores'][opponent],
            'correct_answer': correct_translation,
            'you_answered': True
        }, room=request.sid, namespace='/')
        
        socketio.emit('answer_result', {
            'correct': False,
            'your_score': game['scores'][opponent],
            'opponent_score': game['scores'][request.sid],
            'correct_answer': correct_translation,
            'you_answered': False
        }, room=opponent, namespace='/')

        # Log Word Stats (Wrong)
        with app.app_context():
            db = get_db()
            answering_email = game['emails'].get(request.sid)
            if answering_email:
                 db.execute('''
                    INSERT INTO user_words (user_email, word, correct_count, wrong_count, status, last_seen)
                    VALUES (?, ?, 0, 1, 'learning', CURRENT_TIMESTAMP)
                    ON CONFLICT(user_email, word) DO UPDATE SET
                    wrong_count = wrong_count + 1,
                    status = 'learning',
                    last_seen = CURRENT_TIMESTAMP
                ''', (answering_email, word))
                 db.commit()

        # If both answered wrong
        if len(game['answered']) >= 2 and not game.get('round_over'):
            game['round_over'] = True
            socketio.sleep(2)
            if room_id not in active_games:
                return
                
            word, translation = random.choice(list(WORDS.items()))
            translations = generate_translations(word, num_options=6)

            game['word'] = word
            game['translations'] = translations
            game['answered'] = set()
            game['round_over'] = False

            print(f"DEBUG: Player {request.sid} processing BOTH WRONG - new round in {room_id}")
            socketio.emit('new_round', {
                'word': word,
                'translations': translations
            }, room=room_id, namespace='/')


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

    socketio.emit('game_over', {
        'winner': True,
        'message': 'Соперник сдался! Вы победили!',
        'final_scores': game['scores'],
        'elo_update': None # Simpler to skip complex ELO logic on surrender for MVP or apply penalty later
    }, room=winner, namespace='/')

    socketio.emit('game_over', {
        'winner': False,
        'message': 'Вы сдались.',
        'final_scores': game['scores']
    }, room=loser, namespace='/')

    del active_games[room_id]


if __name__ == '__main__':
    socketio.run(app, debug=True)
