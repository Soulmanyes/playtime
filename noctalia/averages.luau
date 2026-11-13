--!nonstrict
-- Averages tab math: the daemon's summary.json `averages` block holds tracked
-- seconds (overall and per game) and `days`, the span from the first tracked
-- session to now. An average per unit is seconds / days * unit length.

local M = {}

-- Unit name (matches the ui.avg-<name> strings) and its length in days.
M.UNITS = {
  { name = "day", days = 1 },
  { name = "week", days = 7 },
  { name = "month", days = 30.4375 },
  { name = "year", days = 365.25 },
}

function M.rate(seconds, days, unitDays)
  days = tonumber(days) or 0
  if days <= 0 then
    return 0
  end
  return (tonumber(seconds) or 0) / days * unitDays
end

-- "22 Oct 2026", or nil before the first session.
function M.since(avg)
  local t = tonumber(avg and avg.since)
  return t and (os.date("%d %b %Y", t):gsub("^0", "")) or nil
end

-- Fewer days tracked than the unit is long: the average extrapolates.
function M.projected(avg, unit)
  return (tonumber(avg and avg.days) or 0) < unit.days
end

-- Weekday rows from the daemon (Monday first): { days, average } each.
function M.weekdays(avg)
  return (avg and avg.weekdays) or {}
end

-- How full a weekday's bar is: "day" = its average out of 24 hours,
-- "week" = its share of the summed weekday averages (all seven add to 1).
function M.weekdayBar(list, i, mode)
  local seconds = tonumber(list[i] and list[i].average) or 0
  if mode == "day" then
    return math.min(1, seconds / 86400)
  end
  local sum = 0
  for _, w in ipairs(list) do
    sum = sum + (tonumber(w.average) or 0)
  end
  return sum > 0 and seconds / sum or 0
end

-- Whole days tracked, for the "based on N days" note.
function M.days(avg)
  return math.floor(tonumber(avg and avg.days) or 0)
end

return M
