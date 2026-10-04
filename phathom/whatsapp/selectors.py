# Discovered on: 2026-10-02, WhatsApp Web (Chrome 151, title "(36)/(37) WhatsApp")
# Source dumps: data/debug/verify_logged_in.html, 20261002_130018_ring_23_p0.html (voice ring),
#   20261002_131708_in_call_p0.html (voice in-call), 20261002_131851_ring_13_p0.html (video ring, "Video call")
# Call UI opens in the SAME page (no popup/new window; only exception: user-opened settings tab).
# NOTE: class names are obfuscated (x1c4vz4f-style) and change -> role/testid only, never CSS classes.
LOGGED_IN_MARKER = ("css", '[data-testid="chat-list"]')  # chat list pane; seen in verify_logged_in.html
QR_CODE = ("css", "")  # PENDING: only visible when logged out (not observed this session)
INCOMING_CALL_ROOT = ("css", '[data-testid="voip-container-audio-call"], [data-testid="voip-container-incoming-video-call"]')  # voice vs video ring containers
INCOMING_CALLER = ("css", '[data-testid="voip-call-participant-info-name"]')  # held "~Ftm studios" in both rings
INCOMING_IS_VIDEO = ("css", '[data-testid="voip-container-incoming-video-call"]')  # present only for video; call-state-text reads "Video call" vs "Voice call"
ACCEPT_BUTTON = ("role", "^Accept$")  # <button aria-label="Accept"> in both voice + video ring dumps
DECLINE_BUTTON = ("role", "^Decline$")  # <button aria-label="Decline"> in both voice + video ring dumps
ACTIVE_CALL_ROOT = ("css", '[data-testid="voip-container-audio-call"]')  # stays mounted in-call; in-call-only markers: voip-call-timer, voip_voice_call_top_right_overlay (voice in_call dump)
CAMERA_TOGGLE = ("role", "^Turn camera on$")  # video ring UI label; in-call video variant PENDING (voice in-call shows camera-turn-on testid + "Allow camera access..." labels)
CALL_TIMER = ("css", '[data-testid="voip-call-timer"]')  # "0:09" -> "0:10" ticking in in_call dump; a frozen timer = call over
HANGUP_BUTTON = ("role", "^End call$")  # <button aria-label="End call"> in-call btn 27, voice in_call dump
