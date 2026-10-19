import bcrypt
from database import db, User

import base64
import hashlib

# bcrypt only looks at the first 72 BYTES (and bcrypt>=5 raises an error beyond that).
# Dari letters take 2 bytes each, so a 40-character password would already crash.
# New hashes therefore bcrypt a fixed-length SHA-256 digest of the password; they are
# marked with a prefix so old plain-bcrypt hashes keep working.
_PREFIX = 'bs256$'


def _prehash(password):
    digest = hashlib.sha256(password.encode('utf-8')).digest()
    return base64.b64encode(digest)          # 44 ASCII bytes, safely under 72


def hash_password(password):
    return _PREFIX + bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode('utf-8')


def check_password(password, hashed):
    try:
        if hashed.startswith(_PREFIX):
            return bcrypt.checkpw(_prehash(password), hashed[len(_PREFIX):].encode('utf-8'))
        # Legacy hash: plain bcrypt over (at most) the first 72 bytes.
        return bcrypt.checkpw(password.encode('utf-8')[:72], hashed.encode('utf-8'))
    except (ValueError, TypeError):
        return False


# A real hash to compare against when the account does not exist, so "unknown email" and
# "wrong password" take the same time.
_DUMMY_HASH = hash_password('khayyam-timing-equalizer')

def register_user(username, password, email=None, phone=None):
    # check if username taken
    if User.query.filter_by(username=username).first():
        return None, "این نام کاربری قبلاً استفاده شده"

    # check email if provided
    if email:
        if User.query.filter_by(email=email).first():
            return None, "این ایمیل قبلاً ثبت شده"

    # check phone if provided
    if phone:
        if User.query.filter_by(phone=phone).first():
            return None, "این شماره تلفن قبلاً ثبت شده"

    # must have either email or phone
    if not email and not phone:
        return None, "ایمیل یا شماره تلفن الزامی است"

    if email: email = email.strip().lower()
    user = User(
        username=username,
        password_hash=hash_password(password),
        email=email,
        phone=phone
    )
    db.session.add(user)
    db.session.commit()
    return user, None

def login_user_by_email(email, password):
    user = User.query.filter_by(email=email).first()
    if not user:
        check_password(password, _DUMMY_HASH)
        return None, "ایمیل یافت نشد"
    if not check_password(password, user.password_hash):
        return None, "رمز عبور اشتباه است"
    return user, None

def login_user_by_phone(phone, password):
    user = User.query.filter_by(phone=phone).first()
    if not user:
        return None, "شماره تلفن یافت نشد"
    if not check_password(password, user.password_hash):
        return None, "رمز عبور اشتباه است"
    return user, None

def login_user_by_username(identifier, password):
    # try email first, then phone, then username
    user = (
        User.query.filter_by(email=identifier).first() or
        User.query.filter_by(phone=identifier).first() or
        User.query.filter_by(username=identifier).first()
    )
    if not user:
        return None, "کاربر یافت نشد"
    if not check_password(password, user.password_hash):
        return None, "رمز عبور اشتباه است"
    return user, None