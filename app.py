        remaining=min(VOICE_PLANS[active_voice_tier(current_user)]['daily'],
                      quota['remaining_seconds'])
        receipt = URLSafeSerializer(app.secret_key, salt="khayyam-voice-v2").dumps(
            {"uid": current_user.id, "mode": mode, "context": context,
             "sid": session_id, "issued": int(time.time())})
        return jsonify(value=result["value"], expires_at=result.get("expires_at"),
                       receipt=receipt, mode=mode, allowance_seconds=remaining,
                       warning_seconds=VOICE_WARNING_SECONDS,
                       monthly_remaining_seconds=quota['monthly_remaining_seconds'],
                       allowance_deadline=int(now.replace(tzinfo=timezone.utc).timestamp())+remaining)
    except urllib.error.HTTPError as exc:
        app.logger.error("Realtime session creation failed: HTTP %s", exc.code)
        return jsonify(error="Could not start voice session"), 502
    except Exception:
        app.logger.exception("Realtime session creation failed")
        return jsonify(error="Voice service temporarily unavailable"), 503


@app.route("/api/voice/save", methods=["POST"])
@login_required
def save_live_voice_transcript():
    """Idempotent, session-bound voice saves, safe to retry after lost responses."""
    if not current_user.email_verified:
        return jsonify(error="Email verification required"), 403
    data = request.get_json(silent=True) or {}
    try:
        receipt = URLSafeSerializer(app.secret_key, salt="khayyam-voice-v2").loads(
            str(data.get("receipt") or ""))
        issued = int(receipt.get("issued", 0))
        if receipt.get("uid") != current_user.id or not 0 <= time.time() - issued <= 7200:
            return jsonify(error="Voice session expired"), 403
        sid = str(receipt.get("sid") or "")
        if not re.fullmatch(r"[0-9a-f-]{36}", sid):
            return jsonify(error="Unsupported voice session; please restart voice"), 403
        mode = receipt["mode"]
        context = receipt["context"]
    except (BadSignature, ValueError, TypeError, KeyError, AttributeError):
        return jsonify(error="Invalid voice session"), 403

    turns = data.get("turns", [])
    if not isinstance(turns, list) or len(turns) > 30:
        return jsonify(error="Invalid transcript"), 400
    clean = []
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            return jsonify(error="Invalid transcript turn"), 400
        content, turn_id = turn.get("content"), turn.get("turn_id")
        if not isinstance(content, str) or len(content) > 5000 or not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", turn_id):
            return jsonify(error="Invalid transcript content or turn ID"), 400
        if content.strip():
            clean.append({"role": turn["role"], "content": content.strip(), "turn_id": turn_id})
    try:
        _ensure_voice_storage()
        # PostgreSQL row lock serializes concurrent saves for the same session.
        lock = " FOR UPDATE" if db.engine.dialect.name == "postgresql" else ""
        row = db.session.execute(sql_text("""SELECT session_id, user_id, mode, conversation_id
            FROM voice_save_sessions WHERE session_id = :sid""" + lock), {"sid": sid}).mappings().first()
        if not row or row["user_id"] != current_user.id or row["mode"] != mode:
            db.session.rollback()
            return jsonify(error="Voice session not found"), 403
        conv_id = row["conversation_id"]
        if mode == "chat":
            conv = Conversation.query.filter_by(id=conv_id, user_id=current_user.id).first() if conv_id else None
            if conv_id and not conv:
                db.session.rollback()
                return jsonify(error="Conversation not found"), 404
        elif mode == "tutor":
            subject, topic_key, level = context["subject"], context["topic_key"], context["level"]
            if subject not in CURRICULUM or not any(t["key"] == topic_key for t in CURRICULUM[subject]["levels"].get(level, {}).get("topics", [])):
                db.session.rollback()
                return jsonify(error="Lesson not found"), 400
        else:
            db.session.rollback()
            return jsonify(error="Invalid mode"), 400

        # Claim each turn ID inside the same transaction as its message insert.
        new_turns = []
        for turn in clean:
            exists = db.session.execute(sql_text("""SELECT 1 FROM voice_saved_turns
                WHERE session_id=:sid AND turn_id=:tid"""), {"sid": sid, "tid": turn["turn_id"]}).first()
            if exists:
                continue
            db.session.execute(sql_text("""INSERT INTO voice_saved_turns (session_id, turn_id)
                VALUES (:sid, :tid)"""), {"sid": sid, "tid": turn["turn_id"]})
            new_turns.append(turn)

        if mode == "chat":
            if new_turns and not conv:
                title = next((t["content"] for t in new_turns if t["role"] == "user"), "گفتگوی صوتی")
                conv = Conversation(user_id=current_user.id, title=title[:60])
                db.session.add(conv)
                db.session.flush()
                db.session.execute(sql_text("""UPDATE voice_save_sessions
                    SET conversation_id=:cid WHERE session_id=:sid"""), {"cid": conv.id, "sid": sid})
            if new_turns:
                for turn in new_turns:
                    db.session.add(Message(conversation_id=conv.id, role=turn["role"], content=turn["content"]))
                conv.updated_at = datetime.utcnow()
            result = {"saved": len(new_turns), "conversation_id": conv.id if conv else None, "mode": mode}
        else:
            if new_turns:
                progress = TutorProgress.query.filter_by(user_id=current_user.id, subject=subject).first()
                if not progress:
                    progress = TutorProgress(user_id=current_user.id, subject=subject)
                    db.session.add(progress)
                    db.session.flush()
                history = _lesson_history(current_user.id, subject, topic_key, lock=True)
                history.extend({"role": t["role"], "content": t["content"]} for t in new_turns)
                _save_lesson_history(current_user.id, subject, topic_key, history)
                progress.chat_history = json.dumps(history[-60:], ensure_ascii=False)
                progress.last_topic_title = _topic_info(subject, level, topic_key)['title']
                progress.last_activity = datetime.utcnow()
            result = {"saved": len(new_turns), "mode": mode}
        db.session.commit()
        return jsonify(result)
    except Exception:
        db.session.rollback()
        app.logger.exception("Voice transcript save failed")
        return jsonify(error="Voice save failed; please retry"), 503


if __name__ == "__main__":
    app.run(debug=True)