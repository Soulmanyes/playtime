--!nonstrict
-- Drill-down views over one game's tracked session spans (history.json).
-- A view is { level = "year" | "month" | "week" | "day", y, m, d }; each level
-- splits into buckets (months, days, days, hours). A bucket click opens the
-- next level: year > month > day. The week level is not on the drill path
-- but stays available. Weeks start on Monday, as in the daemon's own week
-- period.

local M = {}

M.LEVELS = { "year", "month", "week", "day" }

local WEEKDAYS = { "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun" }
local MONTHS = { "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec" }

-- os.time defaults to noon without an hour; it also normalizes overflowing
-- days and months and follows local DST.
local function at(y, m, d, h)
  return os.time({ year = y, month = m, day = d, hour = h or 0, min = 0, sec = 0 })
end

local function date(t)
  local x = os.date("*t", t)
  return x.year, x.month, x.day
end

local function monday(view)
  local t = at(view.y, view.m, view.d)
  local wday = os.date("*t", t).wday -- 1 = Sunday
  return at(view.y, view.m, view.d - (wday + 5) % 7)
end

function M.today(level)
  local y, m, d = date(os.time())
  return { level = level or "year", y = y, m = m, d = d }
end

-- Edges (epochs, one more than buckets), labels and the view each bucket opens.
function M.layout(view)
  local edges, labels, children = {}, {}, {}
  if view.level == "year" then
    for m = 1, 13 do
      edges[m] = at(view.y, m, 1)
    end
    for m = 1, 12 do
      labels[m] = MONTHS[m]
      children[m] = { level = "month", y = view.y, m = m, d = 1 }
    end
  elseif view.level == "month" then
    local days = os.date("*t", at(view.y, view.m + 1, 0)).day
    for d = 1, days + 1 do
      edges[d] = at(view.y, view.m, d)
    end
    for d = 1, days do
      labels[d] = tostring(d)
      children[d] = { level = "day", y = view.y, m = view.m, d = d }
    end
  elseif view.level == "week" then
    local y, m, d = date(monday(view))
    for i = 1, 8 do
      edges[i] = at(y, m, d + i - 1)
    end
    for i = 1, 7 do
      local cy, cm, cd = date(edges[i])
      labels[i] = WEEKDAYS[i]
      children[i] = { level = "day", y = cy, m = cm, d = cd }
    end
  else
    for h = 1, 25 do
      edges[h] = at(view.y, view.m, view.d, h - 1)
    end
    for h = 1, 24 do
      labels[h] = tostring(h - 1)
    end
  end
  return edges, labels, children
end

-- Seconds of each span inside each bucket. Spans are sorted by start.
function M.buckets(spans, edges)
  local out = {}
  for i = 1, #edges - 1 do
    out[i] = 0
  end
  local lo, hi = edges[1], edges[#edges]
  for _, span in ipairs(spans or {}) do
    local a, b = math.max(tonumber(span[1]) or 0, lo), math.min(tonumber(span[2]) or 0, hi)
    if b > a then
      for i = 1, #edges - 1 do
        local x, y = math.max(a, edges[i]), math.min(b, edges[i + 1])
        if y > x then
          out[i] = out[i] + y - x
        end
      end
    end
  end
  return out
end

function M.title(view)
  if view.level == "year" then
    return tostring(view.y)
  elseif view.level == "month" then
    return MONTHS[view.m] .. " " .. tostring(view.y)
  elseif view.level == "week" then
    local first = monday(view)
    local last = first + 6 * 86400 + 3600 -- +1 h keeps DST weeks on the Sunday
    return os.date("%d %b", first):gsub("^0", "") .. " – " .. os.date("%d %b %Y", last):gsub("^0", "")
  end
  local t = at(view.y, view.m, view.d)
  return (os.date("%a %d %b %Y", t):gsub(" 0", " "))
end

-- The next view up, or nil at the year.
function M.parent(view)
  if view.level == "day" or view.level == "week" then
    return { level = "month", y = view.y, m = view.m, d = view.d }
  elseif view.level == "month" then
    return { level = "year", y = view.y, m = view.m, d = view.d }
  end
  return nil
end

-- The previous (-1) or next (+1) period at the same level.
function M.shift(view, delta)
  local t
  if view.level == "year" then
    t = at(view.y + delta, 1, 1)
  elseif view.level == "month" then
    t = at(view.y, view.m + delta, 1)
  elseif view.level == "week" then
    t = at(view.y, view.m, view.d + 7 * delta)
  else
    t = at(view.y, view.m, view.d + delta)
  end
  local y, m, d = date(t)
  return { level = view.level, y = y, m = m, d = d }
end

return M
