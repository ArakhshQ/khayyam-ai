from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
import json

# expire_on_commit=False: objects (e.g. current_user) are not re-fetched from the database after
# every commit, which saves a network round trip each time on a remote database.
db = SQLAlchemy(session_options={'expire_on_commit': False})

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=True)
    phone = db.Column(db.String(20), unique=True, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    email_verified = db.Column(db.Boolean, default=False)

    chat_plan = db.Column(db.String(20), default='free')
    chat_plan_expires_at = db.Column(db.DateTime, nullable=True)
    tutor_plan = db.Column(db.String(20), default='free')
    tutor_plan_expires_at = db.Column(db.DateTime, nullable=True)

    total_xp = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def plan(self):
        return self.chat_plan or 'free'

    @plan.setter
    def plan(self, value):
        self.chat_plan = value

    conversations = db.relationship('Conversation', backref='user', lazy=True, cascade='all, delete-orphan')
    memories = db.relationship('Memory', backref='user', lazy=True, cascade='all, delete-orphan')
    token_usage = db.relationship('UserTokenUsage', backref='user', lazy=True, cascade='all, delete-orphan')
    tutor_progress = db.relationship('TutorProgress', backref='user', lazy=True, cascade='all, delete-orphan')
    quiz_results = db.relationship('QuizResult', backref='user', lazy=True, cascade='all, delete-orphan')
    badges = db.relationship('StudentBadge', backref='user', lazy=True, cascade='all, delete-orphan')

class Conversation(db.Model):
    __tablename__ = 'conversations'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False, default='گفتگوی جدید')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    summary      = db.Column(db.Text)                               # rolling summary the AI keeps of this chat
    chat_lang    = db.Column(db.String(24))                         # language the user chose for replies (None = not chosen)
    summary_upto = db.Column(db.Integer, default=0)                 # id of the last message the summary covers
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
    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    # general chat tokens
    tier1_tokens       = db.Column(db.Integer, default=0)
    tier1_reset        = db.Column(db.DateTime, default=datetime.utcnow)
    tier2_tokens       = db.Column(db.Integer, default=0)
    tier2_reset        = db.Column(db.DateTime, default=datetime.utcnow)
    tier3_tokens       = db.Column(db.Integer, default=0)
    tier3_reset        = db.Column(db.DateTime, default=datetime.utcnow)
    # tutor-specific tokens (separate pool)
    tutor_tier1_tokens = db.Column(db.Integer, default=0)
    tutor_tier1_reset  = db.Column(db.DateTime, default=datetime.utcnow)
    tutor_tier2_tokens = db.Column(db.Integer, default=0)
    tutor_tier2_reset  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at         = db.Column(db.DateTime, default=datetime.utcnow)

class SiteConfig(db.Model):
    __tablename__ = 'site_config'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class TutorProgress(db.Model):
    __tablename__ = 'tutor_progress'
    id                = db.Column(db.Integer, primary_key=True)
    user_id           = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject           = db.Column(db.String(50), nullable=False)
    current_level     = db.Column(db.Integer, default=1)
    current_topic_idx = db.Column(db.Integer, default=0)
    completed_topics  = db.Column(db.Text, default='[]')
    completed_quizzes = db.Column(db.Text, default='[]')
    subject_xp        = db.Column(db.Integer, default=0)
    chat_history      = db.Column(db.Text, default='[]')
    last_topic_title  = db.Column(db.String(200), default='')
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
    __tablename__ = 'quiz_results'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject      = db.Column(db.String(50), nullable=False)
    level        = db.Column(db.Integer, nullable=False)
    quiz_key     = db.Column(db.String(100), nullable=False)
    score        = db.Column(db.Integer, nullable=False)
    passed       = db.Column(db.Boolean, nullable=False)
    xp_earned    = db.Column(db.Integer, default=0)
    attempted_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'subject':      self.subject,
            'level':        self.level,
            'score':        self.score,
            'passed':       self.passed,
            'xp_earned':    self.xp_earned,
            'attempted_at': self.attempted_at.isoformat(),
        }

class StudentBadge(db.Model):
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

class CallRequest(db.Model):
    """A "call us to buy a plan" request submitted from the pricing page."""
    __tablename__ = 'call_requests'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    name       = db.Column(db.String(120), nullable=False)
    phone      = db.Column(db.String(40), nullable=False)
    plan_kind  = db.Column(db.String(10), nullable=False, default='chat')   # chat | tutor
    plan       = db.Column(db.String(20), nullable=False)
    months     = db.Column(db.Integer, nullable=False, default=1)
    note       = db.Column(db.String(500), nullable=True)
    status     = db.Column(db.String(12), nullable=False, default='new', index=True)  # new | contacted | paid | rejected
    admin_note = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    handled_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id, 'user_id': self.user_id, 'name': self.name,
            'phone': self.phone, 'plan_kind': self.plan_kind, 'plan': self.plan,
            'months': self.months, 'note': self.note or '', 'status': self.status,
            'admin_note': self.admin_note or '',
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'handled_at': self.handled_at.isoformat() if self.handled_at else None,
        }


class PlanEvent(db.Model):
    """Audit log of plan grants, expiries and reminder emails (also dedupes reminders)."""
    __tablename__ = 'plan_events'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    plan_kind  = db.Column(db.String(10), nullable=False)      # chat | tutor
    plan       = db.Column(db.String(20), nullable=False)
    event      = db.Column(db.String(12), nullable=False)      # granted | expired | warned | revoked
    expires_at = db.Column(db.DateTime, nullable=True)
    actor      = db.Column(db.String(60), nullable=True)       # admin username or "system"
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

def json_loads_safe(val):
    try:
        return json.loads(val) if val else []
    except:
        return []

class TutorClass(db.Model):
    """One continuous tutor class: a growing timeline of board beats that can be paused and resumed."""
    __tablename__ = 'tutor_classes'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    subject    = db.Column(db.String(40))
    topic_key  = db.Column(db.String(100))
    title      = db.Column(db.String(200), nullable=False, default='صنف جدید')
    request    = db.Column(db.Text)                       # what the student asked to learn
    outline    = db.Column(db.Text, default='[]')         # JSON list of steps the tutor plans to cover
    notes      = db.Column(db.Text, default='[]')         # JSON list: one line per generated batch of beats
    beats      = db.Column(db.Text, default='[]')         # JSON list of cleaned board beats
    pos        = db.Column(db.Integer, default=0)         # beat the student was on
    step       = db.Column(db.Integer, default=0)         # outline step the tutor is on
    status     = db.Column(db.String(12), default='active')   # active | paused | done
    summary    = db.Column(db.Text)                       # written when the student stops
    next_plan  = db.Column(db.Text)
    gen_until  = db.Column(db.DateTime)                   # lease so two tabs cannot generate at once
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class TutorStudent(db.Model):
    """What the tutor knows about a student's learning, shared by every subject."""
    __tablename__ = 'tutor_students'
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    data       = db.Column(db.Text, default='{}')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
