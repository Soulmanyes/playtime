--!nonstrict
-- Reads the summary.json the playtime daemon writes once a minute. Shared by
-- the bar widget and the panel, which run in separate VMs and each read it.

local M = {}

local stateHome = noctalia.getenv("XDG_STATE_HOME")
if stateHome == nil or stateHome == "" then
  stateHome = "~/.local/state"
end
M.SUMMARY_PATH = stateHome .. "/playtime/summary.json"

-- The daemon replaces the file by atomic rename, so a read never sees half a
-- file; a missing or broken one keeps the previous data.
function M.read(cb, previous)
  local queued = noctalia.readFileAsync(M.SUMMARY_PATH, function(text)
    local data = text and noctalia.json.decode(text)
    cb(type(data) == "table" and data or previous)
  end)
  if not queued then
    cb(previous)
  end
end

function M.duration(seconds)
  local m = math.floor((tonumber(seconds) or 0) / 60)
  if m < 1 then
    return "<1m"
  end
  local h = math.floor(m / 60)
  m = m % 60
  if h > 0 then
    return string.format("%dh %02dm", h, m)
  end
  return tostring(m) .. "m"
end

function M.ago(epoch)
  local s = os.time() - (tonumber(epoch) or 0)
  if s < 90 then
    return "just now"
  elseif s < 5400 then
    return tostring(math.floor(s / 60)) .. " min ago"
  elseif s < 172800 then
    return tostring(math.floor(s / 3600)) .. " h ago"
  end
  return tostring(math.floor(s / 86400)) .. " days ago"
end

-- The daemon rewrites the file every minute; much older means it is not running.
function M.stale(data)
  return data == nil or os.time() - (tonumber(data.updated) or 0) > 300
end

return M
