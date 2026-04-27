from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(150), unique=True, nullable=True)
    phone         = db.Column(db.String(20), unique=True, nullable=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    plan          = db.Column(db.String(20), default='free')  # free, basic, pro, premium
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    conversations = db.relationship('Conversation', backref='user', lazy=True, cascade='all, delete-orphan')
    memories      = db.relationship('Memory', backref='user', lazy=True, cascade='all, delete-orphan')
    token_usage   = db.relationship('UserTokenUsage', backref='user', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<User {self.username}>'

class Conversation(db.Model):
    __tablename__ = 'conversations'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False, default='گفتگوی جدید')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    messages   = db.relationship('Message', backref='conversation', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id':         self.id,
            'title':      self.title,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'messages':   [m.to_dict() for m in self.messages]
        }

class Message(db.Model):
    __tablename__ = 'messages'

    id              = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey('conversations.id'), nullable=False)
    role            = db.Column(db.String(20), nullable=False)
    content         = db.Column(db.Text, nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'role':    self.role,
            'content': self.content
        }

class Memory(db.Model):
    __tablename__ = 'memories'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':         self.id,
            'content':    self.content,
            'created_at': self.created_at.isoformat()
        }

class UserTokenUsage(db.Model):
    __tablename__ = 'user_token_usage'

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    # tier 1 tokens (GPT-5.4 for premium, Mini for others)
    tier1_tokens  = db.Column(db.Integer, default=0)
    tier1_reset   = db.Column(db.DateTime, default=datetime.utcnow)
    # tier 2 tokens (Mini for premium, Nano for others)
    tier2_tokens  = db.Column(db.Integer, default=0)
    tier2_reset   = db.Column(db.DateTime, default=datetime.utcnow)
    # tier 3 tokens (Nano for premium only)
    tier3_tokens  = db.Column(db.Integer, default=0)
    tier3_reset   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'tier1_tokens': self.tier1_tokens,
            'tier1_reset':  self.tier1_reset.isoformat(),
            'tier2_tokens': self.tier2_tokens,
            'tier2_reset':  self.tier2_reset.isoformat(),
            'tier3_tokens': self.tier3_tokens,
            'tier3_reset':  self.tier3_reset.isoformat(),
        }
class SiteConfig(db.Model):
    __tablename__ = 'site_config'

    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'key':        self.key,
            'value':      self.value,
            'updated_at': self.updated_at.isoformat()
        }