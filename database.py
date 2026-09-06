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
    plan          = db.Column(db.String(20), default='free')
    total_xp      = db.Column(db.Integer, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    conversations  = db.relationship('Conversation', backref='user', lazy=True, cascade='all, delete-orphan')
    memories       = db.relationship('Memory', backref='user', lazy=True, cascade='all, delete-orphan')
    token_usage    = db.relationship('UserTokenUsage', backref='user', lazy=True, cascade='all, delete-orphan')
    tutor_progress = db.relationship('TutorProgress', backref='user', lazy=True, cascade='all, delete-orphan')
    quiz_results   = db.relationship('QuizResult', backref='user', lazy=True, cascade='all, delete-orphan')
    badges         = db.relationship('StudentBadge', backref='user', lazy=True, cascade='all, delete-orphan')

class Conversation(db.Model):
    __tablename__ = 'conversations'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False, default='گفتگوی جدید')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
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
        return {'role': self.role, 'content': self.content}

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
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    tier1_tokens = db.Column(db.Integer, default=0)
    tier1_reset  = db.Column(db.DateTime, default=datetime.utcnow)
    tier2_tokens = db.Column(db.Integer, default=0)
    tier2_reset  = db.Column(db.DateTime, default=datetime.utcnow)
    tier3_tokens = db.Column(db.Integer, default=0)
    tier3_reset  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow)

class SiteConfig(db.Model):
    __tablename__ = 'site_config'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

# ── TUTOR TABLES ──

class TutorProgress(db.Model):
    """Tracks exactly where each student is in each subject."""
    __tablename__ = 'tutor_progress'
    id                = db.Column(db.Integer, primary_key=True)
    user_id           = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject           = db.Column(db.String(50), nullable=False)   # 'math', 'science', 'dari', 'english', 'computer'
    current_level     = db.Column(db.Integer, default=1)           # 1-4
    current_topic_idx = db.Column(db.Integer, default=0)           # index in curriculum
    completed_topics  = db.Column(db.Text, default='[]')           # JSON array of completed topic keys
    completed_quizzes = db.Column(db.Text, default='[]')           # JSON array of completed quiz keys
    subject_xp        = db.Column(db.Integer, default=0)
    chat_history      = db.Column(db.Text, default='[]')           # JSON — full chat for this subject
    last_topic_title  = db.Column(db.String(200), default='')      # last topic they were on
    last_activity     = db.Column(db.DateTime, default=datetime.utcnow)
    started_at        = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('user_id', 'subject', name='_user_subject_uc'),)

    def to_dict(self):
        return {
            'subject':           self.subject,
            'current_level':     self.current_level,
            'current_topic_idx': self.current_topic_idx,
            'completed_topics':  json_loads_safe(self.completed_topics),
            'completed_quizzes': json_loads_safe(self.completed_quizzes),
            'subject_xp':        self.subject_xp,
            'last_topic_title':  self.last_topic_title,
            'last_activity':     self.last_activity.isoformat(),
        }

class QuizResult(db.Model):
    """Every quiz attempt ever made."""
    __tablename__ = 'quiz_results'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject      = db.Column(db.String(50), nullable=False)
    level        = db.Column(db.Integer, nullable=False)
    quiz_key     = db.Column(db.String(100), nullable=False)  # e.g. "math_level1"
    score        = db.Column(db.Integer, nullable=False)       # percentage 0-100
    passed       = db.Column(db.Boolean, nullable=False)
    xp_earned    = db.Column(db.Integer, default=0)
    attempted_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'subject':      self.subject,
            'level':        self.level,
            'quiz_key':     self.quiz_key,
            'score':        self.score,
            'passed':       self.passed,
            'xp_earned':    self.xp_earned,
            'attempted_at': self.attempted_at.isoformat(),
        }

class StudentBadge(db.Model):
    """Achievements and rewards."""
    __tablename__ = 'student_badges'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    badge_key   = db.Column(db.String(100), nullable=False)
    badge_title = db.Column(db.String(200), nullable=False)
    badge_emoji = db.Column(db.String(10), nullable=False)
    subject     = db.Column(db.String(50), nullable=True)
    earned_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'badge_key':   self.badge_key,
            'badge_title': self.badge_title,
            'badge_emoji': self.badge_emoji,
            'subject':     self.subject,
            'earned_at':   self.earned_at.isoformat(),
        }

def json_loads_safe(val):
    try:
        import json
        return json.loads(val) if val else []
    except:
        return []