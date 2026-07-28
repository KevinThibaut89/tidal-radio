# tidal-radio audio pipeline (liquidsoap 2.x)
# The Python brain pushes local files (music + voice breaks) onto the "main"
# queue over telnet; we crossfade and stream to icecast.

settings.server.telnet := true
settings.server.telnet.bind_addr := "127.0.0.1"
settings.server.telnet.port := 1234

icecast_pass = environment.get(default="hackme", "ICECAST_PASS")

main = request.queue(id="main")

# Gentle radio-style crossfade between items. Voice breaks are short files and
# get the same treatment, which sounds like a DJ talking over the fade.
radio = crossfade(fade_in=1.0, fade_out=2.5, duration=3.0, main)

# Never go silent if the brain stalls.
radio = mksafe(radio)

output.icecast(
  %mp3(bitrate=192),
  host="127.0.0.1", port=8000, password=icecast_pass,
  mount="radio.mp3",
  name="Tidal Radio", description="Personal AI radio",
  radio
)
