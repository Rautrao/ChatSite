import os
import threading
from dotenv import load_dotenv
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
)
from utils import hash_password, generate_salt

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default_secret_key')

# threading mode: no eventlet/gevent required, works on any Python version
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", ping_interval=10, ping_timeout=20)

class RoomError(Exception):
    pass

class ChatData:
    def __init__(self):
        self.online_users = {}  # username -> sid
        self.sid_to_username = {} # sid -> username
        self.chatrooms = {"lobby": []}  # room_name -> [user1, user2, ...]
        self.chatrooms["example"] = []  # for demo
        self.lock = threading.RLock()
        
        DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+pysqlite:///chatapp.db")
        self.engine = create_engine(DATABASE_URL)
        metadata_obj = MetaData()
        self.users = Table(
            "users",
            metadata_obj,
            Column("id", Integer, primary_key=True),
            Column("username", String, nullable=False),
            Column("password", String, nullable=False),
            Column("salt", String, nullable=False),
        )
        metadata_obj.create_all(self.engine)

    def is_registered(self, username: str):
        stmt = self.users.select().where(self.users.c.username == username)
        with self.engine.connect() as conn:
            result = conn.execute(stmt).fetchone()
            return result is not None

    def add_user(self, username, password):
        salt = generate_salt()
        stmt = self.users.insert().values(
            username=username, password=hash_password(password, salt), salt=salt
        )
        with self.engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()

    def check_password(self, username, password):
        stmt = self.users.select().where(self.users.c.username == username)
        with self.engine.connect() as conn:
            result = conn.execute(stmt).fetchone()
            if not result:
                return False
            return result.password == hash_password(password, result.salt)

    def add_online_user(self, username, sid):
        with self.lock:
            self.online_users[username] = sid
            self.sid_to_username[sid] = username

    def remove_online_user(self, sid):
        with self.lock:
            username = self.sid_to_username.pop(sid, None)
            if username:
                self.online_users.pop(username, None)
                # Remove from all rooms
                for room, users in self.chatrooms.items():
                    if username in users:
                        users.remove(username)
            return username

    def enter_room(self, username, destination, source=None):
        with self.lock:
            if destination not in self.chatrooms:
                raise RoomError(f"Destination room {destination} not found")
            if source and source in self.chatrooms and username in self.chatrooms[source]:
                self.chatrooms[source].remove(username)
            if username not in self.chatrooms[destination]:
                self.chatrooms[destination].append(username)

    def create_room(self, room_name):
        with self.lock:
            if room_name in self.chatrooms:
                raise RoomError(f"{room_name} already exists")
            self.chatrooms[room_name] = []

    def get_room_users(self, room_name) -> list:
        with self.lock:
            return list(self.chatrooms.get(room_name, []))

    def get_sid(self, username):
        with self.lock:
            return self.online_users.get(username, None)

    def get_room_info(self, room_name=None):
        with self.lock:
            if not room_name:
                return {room: list(users) for room, users in self.chatrooms.items()}
            if room_name not in self.chatrooms:
                return {room_name: []}
            return {room_name: list(self.chatrooms[room_name])}

chat_data = ChatData()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def on_connect():
    pass

@socketio.on('disconnect')
def on_disconnect():
    username = chat_data.remove_online_user(request.sid)
    if username:
        broadcast_lobby_state()

@socketio.on('register')
def on_register(data):
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        emit('register_ack', {'status': 'error', 'message': 'Username and password required'})
        return
        
    if chat_data.is_registered(username):
        emit('register_ack', {'status': 'error', 'message': 'Username already in use'})
    else:
        chat_data.add_user(username, password)
        emit('register_ack', {'status': 'ok', 'message': 'Registered successfully'})

@socketio.on('login')
def on_login(data):
    username = data.get('username')
    password = data.get('password')
    if not chat_data.is_registered(username):
        emit('login_ack', {'status': 'error', 'message': 'Username not found'})
        return
    if not chat_data.check_password(username, password):
        emit('login_ack', {'status': 'error', 'message': 'Invalid password'})
        return
        
    chat_data.add_online_user(username, request.sid)
    try:
        chat_data.enter_room(username, 'lobby')
    except Exception:
        pass
        
    emit('login_ack', {
        'status': 'ok', 
        'message': 'Login successful', 
        'data': {'username': username, 'chatroom': 'lobby'}
    })
    broadcast_lobby_state()

@socketio.on('logout')
def on_logout():
    username = chat_data.remove_online_user(request.sid)
    emit('logout_ack', {'status': 'ok', 'message': 'Logged out successfully'})
    if username:
        broadcast_lobby_state()

@socketio.on('create_room')
def on_create_room(data):
    room_name = data.get('room')
    if not room_name:
        return
    try:
        chat_data.create_room(room_name)
        emit('create_room_ack', {'status': 'ok', 'message': f'Room {room_name} created'})
        broadcast_lobby_state()
    except RoomError as e:
        emit('create_room_ack', {'status': 'error', 'message': str(e)})

@socketio.on('enter_room')
def on_enter_room(data):
    username = chat_data.sid_to_username.get(request.sid)
    room_name = data.get('room')
    current_room = data.get('current_room', 'lobby')
    
    if not username or not room_name:
        return
        
    try:
        chat_data.enter_room(username, room_name, current_room)
        emit('enter_room_ack', {
            'status': 'ok', 
            'message': f'Entered {room_name}',
            'data': {'room': room_name}
        })
        broadcast_lobby_state()
        broadcast_room_state(room_name)
        if current_room:
            broadcast_room_state(current_room)
    except RoomError as e:
        emit('enter_room_ack', {'status': 'error', 'message': str(e)})

@socketio.on('leave_room')
def on_leave_room(data):
    username = chat_data.sid_to_username.get(request.sid)
    room_name = data.get('room')
    if not username or not room_name:
        return
        
    try:
        chat_data.enter_room(username, 'lobby', room_name)
        emit('leave_room_ack', {'status': 'ok', 'message': f'Left {room_name}'})
        broadcast_lobby_state()
        broadcast_room_state(room_name)
    except RoomError as e:
        emit('leave_room_ack', {'status': 'error', 'message': str(e)})

@socketio.on('msg')
def on_message(data):
    username = chat_data.sid_to_username.get(request.sid)
    if not username:
        return
        
    text = data.get('text')
    receiver = data.get('to')
    room_name = data.get('room')
    
    if receiver == 'public':
        # Send to everyone in the room
        room_users = chat_data.get_room_users(room_name)
        for u in room_users:
            sid = chat_data.get_sid(u)
            if sid:
                emit('msg', {'from': username, 'to': 'public', 'text': text}, to=sid)
    else:
        # Private message
        target_sid = chat_data.get_sid(receiver)
        if target_sid:
            emit('msg', {'from': username, 'to': receiver, 'text': text}, to=target_sid)
            # Send copy to sender
            emit('msg', {'from': username, 'to': username, 'text': text})
        else:
            emit('error', {'message': f'User {receiver} not found or offline'})

@socketio.on('list_lobby')
def on_list_lobby():
    info = chat_data.get_room_info()
    emit('list_lobby_ack', {'data': info})

@socketio.on('list_room')
def on_list_room(data):
    room_name = data.get('room')
    if room_name:
        info = chat_data.get_room_info(room_name)
        emit('list_room_ack', {'data': info})

def broadcast_lobby_state():
    info = chat_data.get_room_info()
    for username in chat_data.get_room_users('lobby'):
        sid = chat_data.get_sid(username)
        if sid:
            socketio.emit('list_lobby_ack', {'data': info}, to=sid)

def broadcast_room_state(room_name):
    info = chat_data.get_room_info(room_name)
    for username in chat_data.get_room_users(room_name):
        sid = chat_data.get_sid(username)
        if sid:
            socketio.emit('list_room_ack', {'data': info}, to=sid)

if __name__ == '__main__':
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "65432"))
    print(f"Starting server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=True)
