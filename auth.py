import bcrypt
from database import db, User

def hash_password(password):
    return bcrypt.hashpw(
        password.encode('utf-8'),
        bcrypt.gensalt()
    ).decode('utf-8')

def check_password(password, hashed):
    return bcrypt.checkpw(
        password.encode('utf-8'),
        hashed.encode('utf-8')
    )

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