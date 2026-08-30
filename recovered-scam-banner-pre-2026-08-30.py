import datetime
import re

async def run():
    now = datetime.datetime.now(datetime.timezone.utc)
    author_id = context["author_id"]
    
    # 1. Track last seen / First message / Inactivity (30 days)
    last_seen_str = await memory_get(author_id)
    is_returning_or_new_speaker = False
    
    if last_seen_str is None:
        is_returning_or_new_speaker = True
    else:
        try:
            last_seen = datetime.datetime.fromisoformat(last_seen_str)
            # 30 days = 2,592,000 seconds
            if (now - last_seen).total_seconds() > 2592000:
                is_returning_or_new_speaker = True
        except Exception:
            is_returning_or_new_speaker = True
            
    # Update timestamp for everyone so we track their activity
    await memory_set(author_id, now.isoformat())

    # 2. Check Join Age (First 24 hours)
    is_new_account = False
    joined_at_str = context.get("author_joined_at")
    if joined_at_str:
        try:
            # Handle ISO 8601 parsing
            joined_at = datetime.datetime.fromisoformat(joined_at_str.replace("Z", "+00:00"))
            if (now - joined_at).total_seconds() < 86400:
                is_new_account = True
        except Exception:
            pass

    # Guard: only scan if user meets one of the "suspicious profile" criteria
    if not (is_returning_or_new_speaker or is_new_account):
        return

    content = context.get("message_content", "")
    attachments = context.get("attachments", [])
    
    # 3. Heuristic check to see if it's worth judging
    # Triggers: has attachments, contains a link, or matches scammy/abusive keywords
    suspicious_patterns = [r"http", r"crypto", r"wallet", r"airdrop", r"claim", r"mint", r"free", r"gift", r"nigger", r"faggot"]
    is_suspicious = any(re.search(p, content, re.I) for p in suspicious_patterns) or len(attachments) > 0
    
    if not is_suspicious:
        return

    # 4. Judge Agent: Analyze context and images
    attachment_urls = [a["url"] for a in attachments]
    context_label = "new member" if is_new_account else "returning/first-time speaker"
    
    prompt = (
        f"Analyze this message from a {context_label}.\n"
        f"Content: {content}\n"
        f"Attachment URLs: {attachment_urls}\n\n"
        "Instructions: Check for crypto scams, phishing, slurs, or egregious vulgarity. "
        "Inspect image attachments for scam text or inappropriate content. "
        "If it is a clear violation, reply 'VIOLATION: [reason]'. "
        "If it is harmless, reply 'OK'."
    )
    
    judgment = await spawn_agent(prompt, has_tools=True)
    
    # 5. Action: Ban and Log
    if "VIOLATION" in judgment.upper():
        user_name = context["author_name"]
        msg_id = context["message_id"]
        reason = judgment.strip()

        # Ban and Delete
        await ban_user(author_id, reason=f"Automated Scan ({context_label}): {reason}")
        await delete_message(msg_id)

        # Log to mod-log channel
        log_channel = "728249959098482829"
        log_text = (
            f"🛡️ **Scam/Abuse Ban**\n"
            f"**User:** {user_name} ({author_id})\n"
            f"**Category:** {context_label.title()}\n"
            f"**Violation:** {reason}\n"
            f"**Original Message:** {content[:400]}"
        )
        await send_message(log_text, channel_id=log_channel)

await run()
