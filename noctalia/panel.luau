--!nonstrict
-- Playtime panel: tabs for day/week/month/year/all time, a bar chart of the
-- period's buckets (hours, days, months or years) and per-game totals.
-- Clicking a game opens its own chart, starting at this year; clicking a bar
-- drills down to month and day (see history.luau).

local shared = require("./shared.luau")
local history = require("./history.luau")
local averages = require("./averages.luau")

local TABS = { "day", "week", "month", "year", "all", "avg" }
local CHART_HEIGHT = 180
local CHART_WIDTH = 840
-- Sparse x labels so 24 hours or 31 days stay readable.
local LABEL_EVERY = { day = 6, week = 1, month = 5, year = 1, all = 1 }
-- All time holds every Steam game ever played; a full list is slow to render.
local MAX_GAMES = 50

local data = nil
local open = false
-- Game view: the selected game ({ key, name }), the shown period and the
-- daemon's history.json. nil selected = the normal per-period list.
local selected = nil
local view = nil
local hist = nil
-- Key of the clickable node under the pointer (game row or chart bar).
local hovered = nil

-- onHover handler that tracks `hovered` and redraws when it changes. Hover
-- styles must switch to an explicit idle value (e.g. a clear fill), never nil:
-- a nil prop keeps the node's previous value, so the highlight would stick.
local function hover(key)
  return function(state)
    if state == "true" or state == true then
      hovered = key
    elseif hovered == key then
      hovered = nil
    else
      return
    end
    render()
  end
end

local function currentTab()
  local t = noctalia.state.get("tab")
  for _, name in ipairs(TABS) do
    if name == t then
      return t
    end
  end
  return "week"
end

-- Clickable things use Noctalia's own buttons, which bring their hover and
-- press states; the selected tab is the filled primary one.
local function tabRow(current)
  local tabs = {}
  for _, name in ipairs(TABS) do
    local active = name == current
    tabs[#tabs + 1] = ui.button({
      -- The state is in the key so the button is rebuilt with its new look.
      key = "tab-" .. name .. (active and "-on" or ""),
      text = noctalia.tr("ui.tab-" .. name),
      variant = active and "primary" or "outline",
      flexGrow = 1,
      onClick = function()
        noctalia.state.set("tab", name)
        render()
      end,
    })
  end
  return ui.row({ gap = 4 }, tabs)
end

-- Y-axis steps: round minutes, then round hours, then 1/2/5 x 10^k hours.
local STEPS = { 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400 }
local MAX_TICKS = 4
local LABEL_H = 18 -- every axis label sits in a box this tall, so rows line up
local Y_AXIS_W = 44

-- The smallest round step that covers `peak` in at most MAX_TICKS steps.
local function axisStep(peak)
  for _, step in ipairs(STEPS) do
    if peak <= step * MAX_TICKS then
      return step
    end
  end
  local step = 100 * 3600
  while true do
    for _, f in ipairs({ 1, 2, 5 }) do
      if peak <= f * step * MAX_TICKS then
        return f * step
      end
    end
    step = step * 10
  end
end

-- "0", "30m", "1h", "1h30".
local function tickLabel(seconds)
  local m = math.floor(seconds / 60)
  local h = math.floor(m / 60)
  m = m % 60
  if h == 0 then
    return m == 0 and "0" or tostring(m) .. "m"
  end
  return m == 0 and tostring(h) .. "h" or string.format("%dh%02d", h, m)
end

local function axisLabel(text, width, align)
  return ui.row({ width = width, height = LABEL_H, align = "center", justify = align, flexShrink = 0 }, {
    ui.label({ text = text, fontSize = 11, color = "on_surface_variant", maxLines = 1 }),
  })
end

-- Bar chart with a time y-axis (round ticks, bars scaled to the top tick) and
-- the bucket labels as x-axis. onBar(i), when given, makes each bar
-- clickable. Every height is fixed, so tick labels centre on their values:
--   y-axis column:  tick 0 .. tick K, LABEL_H boxes spaced CHART_HEIGHT / K
--   plot column:    LABEL_H / 2 top pad, bars, 1 px baseline, x labels
-- X labels sit in boxes as wide as the bars, so each column is as wide as its
-- bar's and the labels stay under their bars.
local function chart(tab, buckets, onBar)
  local peak = 0
  for _, b in ipairs(buckets) do
    peak = math.max(peak, tonumber(b.seconds) or 0)
  end
  local step = axisStep(math.max(peak, 1))
  local ticks = math.max(1, math.ceil(peak / step))
  local top = step * ticks
  local n = math.max(1, #buckets)
  local barWidth = math.max(6, math.min(40, math.floor((CHART_WIDTH - Y_AXIS_W) / n) - 3))
  local every = LABEL_EVERY[tab] or 1

  local bars, xLabels = {}, {}
  for i, b in ipairs(buckets) do
    local seconds = tonumber(b.seconds) or 0
    local key = "bar-" .. tab .. "-" .. tostring(i)
    -- Clickable bars light up under the pointer, column and bar both.
    local lit = onBar ~= nil and hovered == key
    bars[#bars + 1] = ui.column({
      key = key,
      align = "center",
      flexGrow = 1,
      height = CHART_HEIGHT,
      radius = 4,
      fill = lit and "on_surface/0.10" or "on_surface/0",
      tooltip = tostring(b.label) .. " · " .. shared.duration(seconds),
      onHover = onBar and hover(key) or nil,
      onClick = onBar and function()
        onBar(i)
      end or nil,
    }, {
      ui.spacer({ flexGrow = 1 }),
      ui.box({
        width = barWidth,
        flexShrink = 0,
        height = seconds > 0 and math.max(3, math.floor(CHART_HEIGHT * seconds / top)) or 0,
        radius = 4,
        fill = lit and "secondary" or "primary",
      }),
    })
    xLabels[#xLabels + 1] = ui.column({ key = "x-" .. tab .. "-" .. tostring(i), align = "center", flexGrow = 1 }, {
      axisLabel((i - 1) % every == 0 and tostring(b.label) or " ", barWidth, "center"),
    })
  end

  local yTicks = {}
  for k = ticks, 0, -1 do
    yTicks[#yTicks + 1] = axisLabel(tickLabel(k * step), Y_AXIS_W, "end")
    if k > 0 then
      yTicks[#yTicks + 1] = ui.spacer({ height = CHART_HEIGHT / ticks - LABEL_H, flexShrink = 0, flexGrow = 0 })
    end
  end

  return ui.row({ gap = 6, align = "start" }, {
    ui.column({ width = Y_AXIS_W, flexShrink = 0 }, yTicks),
    ui.column({ flexGrow = 1, align = "stretch" }, {
      ui.spacer({ height = LABEL_H / 2, flexShrink = 0, flexGrow = 0 }),
      ui.row({ height = CHART_HEIGHT, align = "end" }, {
        ui.box({ width = 1, height = CHART_HEIGHT, flexShrink = 0, fill = "outline/0.5" }),
        ui.row({ gap = 2, flexGrow = 1, height = CHART_HEIGHT, align = "end" }, bars),
      }),
      ui.box({ height = 1, flexShrink = 0, fill = "outline/0.5" }),
      ui.row({}, {
        ui.spacer({ width = 1, flexShrink = 0, flexGrow = 0 }),
        ui.row({ gap = 2, flexGrow = 1 }, xLabels),
      }),
    }),
  })
end

local function gameRow(game, total)
  local seconds = tonumber(game.seconds) or 0
  local details = {}
  for _, d in ipairs(game.details or {}) do
    details[#details + 1] = tostring(d.name) .. " · " .. shared.duration(d.seconds)
  end
  local children = {
    ui.row({ align = "center", gap = 8 }, {
      ui.label({ text = tostring(game.name), fontSize = 13, flexGrow = 1, maxLines = 1 }),
      ui.label({ text = shared.duration(seconds), fontSize = 13, fontWeight = "bold" }),
    }),
    ui.progress({ progress = total > 0 and seconds / total or 0, height = 5, radius = 2,
                  track = "on_surface/0.22" }),
  }
  if #details > 0 then
    children[#children + 1] = ui.label({ text = table.concat(details, "   "), fontSize = 11,
                                         color = "on_surface_variant", maxLines = 2 })
  end
  local key = "game-" .. tostring(game.key or game.name)
  local lit = hovered == key
  -- The border is always there (clear when idle) so hovering doesn't shift rows.
  return ui.column({
    key = key,
    gap = 4,
    paddingH = 8,
    paddingV = 6,
    radius = 8,
    fill = lit and "surface_variant/0.9" or "surface_variant/0",
    border = lit and "primary/0.85" or "outline/0",
    borderWidth = 1,
    onHover = hover(key),
    onClick = function()
      selectGame(game)
    end,
  }, children)
end

-- Update line in the footer: after an update, while one waits or runs.
local updating = false
local updateError = nil

local function runUpdate()
  updating, updateError = true, nil
  local script = tostring(noctalia.getenv("HOME")) .. "/.local/lib/playtime/playtime.py"
  local started = noctalia.runAsync({ "python3", "-I", script, "update" }, function(r)
    updating = false
    if r.exitCode ~= 0 then
      local lines = {}
      for line in tostring(r.stderr or ""):gmatch("[^\n]+") do
        lines[#lines + 1] = line
      end
      updateError = lines[#lines] or ("exit " .. tostring(r.exitCode))
    end
    render()
  end, 180000)
  if not started then
    updating, updateError = false, "could not start playtime update"
  end
  render()
end

local function updateRow()
  local u = data.update
  if type(u) ~= "table" then
    return nil
  end
  local function note(text, color)
    return ui.label({ text = text, fontSize = 11, color = color or "on_surface_variant", maxLines = 2 })
  end
  if updating then
    return note(noctalia.tr("ui.update-running"))
  end
  if updateError then
    return note(noctalia.tr("ui.update-error", { error = updateError }), "error")
  end
  local tag = tostring(u.available or "")
  if tag ~= "" and not u.dev then
    local text
    if u.failed then
      text = noctalia.tr("ui.update-failed", { tag = tag })
    elseif u.auto then
      text = noctalia.tr("ui.update-auto", { tag = tag })
    else
      text = noctalia.tr("ui.update-available", { tag = tag })
    end
    return ui.row({ align = "center", gap = 8 }, {
      ui.label({ text = text, fontSize = 11, color = u.failed and "error" or "on_surface_variant",
                 flexGrow = 1, maxLines = 2 }),
      ui.button({ key = "update", text = noctalia.tr("ui.update-button"), variant = "primary",
                  onClick = runUpdate }),
    })
  end
  if tostring(u.updated_from or "") ~= "" then
    return note(noctalia.tr("ui.updated", { version = tostring(u.version), from = tostring(u.updated_from) }))
  end
  return nil
end

local function footer()
  local sync = data.sync or {}
  local text, color
  if sync.enabled == false then
    local b = tonumber((data.backup or {}).ok) or 0
    if b > 0 then
      text = noctalia.tr("ui.local-only-backup", { ago = shared.ago(b) })
    else
      text = noctalia.tr("ui.local-only")
    end
    color = "on_surface_variant"
  elseif sync.error ~= nil and sync.error ~= "" then
    text, color = noctalia.tr("ui.sync-error", { error = sync.error }), "error"
  elseif (tonumber(sync.ok) or 0) > 0 then
    text, color = noctalia.tr("ui.synced", { ago = shared.ago(sync.ok) }), "on_surface_variant"
  else
    text, color = noctalia.tr("ui.never-synced"), "on_surface_variant"
  end
  local rows = { ui.label({ text = text, fontSize = 11, color = color, maxLines = 2 }) }
  if shared.stale(data) then
    rows[#rows + 1] = ui.label({ text = noctalia.tr("ui.stale", { ago = shared.ago(data.updated) }),
                                 fontSize = 11, color = "error" })
  end
  local update = updateRow()
  if update then
    rows[#rows + 1] = update
  end
  return ui.column({ gap = 2 }, rows)
end

local function loadHistory()
  shared.readHistory(function(h)
    hist = h
    render()
  end, hist)
end

function selectGame(game)
  selected = { key = tostring(game.key or game.name), name = tostring(game.name) }
  view = history.today("year")
  loadHistory()
  render()
end

local function leaveGame()
  selected, view = nil, nil
  render()
end

local function goUp()
  local up = history.parent(view)
  if up then
    view = up
    render()
  else
    leaveGame()
  end
end

local function navButton(key, glyph, onClick)
  return ui.button({ key = key, glyph = glyph, variant = "default", onClick = onClick })
end

local function gameView(header)
  local game = hist and hist.games and hist.games[selected.key] or {}
  local edges, labels, children = history.layout(view)
  local seconds = history.buckets(game.spans, edges)
  local buckets, total = {}, 0
  for i, s in ipairs(seconds) do
    buckets[i] = { label = labels[i], seconds = s }
    total = total + s
  end
  local up = history.parent(view)
  local body = {
    header,
    ui.row({ align = "center", gap = 6 }, {
      ui.button({ key = "up-" .. view.level, glyph = "chevron-left", variant = "default",
                  text = up and history.title(up) or noctalia.tr("ui.all-games"), onClick = goUp }),
      ui.spacer({ flexGrow = 1 }),
      ui.label({ text = tostring(game.name or selected.name), fontSize = 14, fontWeight = "bold", maxLines = 1 }),
    }),
    ui.row({ align = "center", gap = 6 }, {
      navButton("prev", "chevron-left", function()
        view = history.shift(view, -1)
        render()
      end),
      ui.label({ text = history.title(view), fontSize = 13, flexGrow = 1,
                 textAlign = "center" }),
      navButton("next", "chevron-right", function()
        view = history.shift(view, 1)
        render()
      end),
    }),
    ui.row({ align = "center" }, {
      ui.label({ text = noctalia.tr("ui.total"), color = "on_surface_variant", flexGrow = 1 }),
      ui.label({ text = shared.duration(total), fontSize = 18, fontWeight = "bold" }),
    }),
    chart(view.level, buckets, children[1] and function(i)
      view = children[i]
      render()
    end or nil),
  }
  if hist == nil then
    body[#body + 1] = ui.label({ text = noctalia.tr("ui.no-history"), fontSize = 11,
                                 color = "on_surface_variant" })
  end
  local untracked = tonumber(game.untracked) or 0
  if untracked > 0 then
    body[#body + 1] = ui.label({ text = noctalia.tr("ui.untracked", { time = shared.duration(untracked) }),
                                 fontSize = 11, color = "on_surface_variant", maxLines = 3 })
  end
  body[#body + 1] = ui.spacer({ flexGrow = 1 })
  body[#body + 1] = footer()
  panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1, align = "stretch" }, body))
end

-- Averages tab: one card per unit for all games, then each game's averages
-- in columns. Rows open the game view like the period lists do.
local AVG_COL_W = 104

local function avgCard(unit, avg)
  return ui.column({ key = "avg-card-" .. unit.name, flexGrow = 1, gap = 2, padding = 10, radius = 10,
                     fill = "surface_variant/0.9", border = "outline/0.6", borderWidth = 1 }, {
    ui.label({ text = noctalia.tr("ui.avg-" .. unit.name)
                     .. (averages.projected(avg, unit) and " " .. noctalia.tr("ui.avg-projected") or ""),
               fontSize = 11, color = "on_surface_variant" }),
    ui.label({ text = shared.duration(averages.rate(avg.total, avg.days, unit.days)),
               fontSize = 20, fontWeight = "bold" }),
  })
end

-- Header cells mark projected units with "(proj.)"; the cards spell it out.
local function avgCells(seconds, days, opts)
  local cells = {}
  for _, unit in ipairs(averages.UNITS) do
    local text
    if opts.header then
      text = noctalia.tr("ui.avg-" .. unit.name)
      if averages.projected({ days = days }, unit) then
        text = text .. " " .. noctalia.tr("ui.avg-proj-short")
      end
    else
      text = shared.duration(averages.rate(seconds, days, unit.days))
    end
    cells[#cells + 1] = ui.row({ width = AVG_COL_W, flexShrink = 0, justify = "end" }, {
      ui.label({ text = text,
                 fontSize = opts.header and 11 or 13, color = opts.header and "on_surface_variant" or nil,
                 fontWeight = opts.bold and "bold" or nil, maxLines = 1 }),
    })
  end
  return cells
end

local function avgGameRow(game, avg)
  local key = "avg-game-" .. tostring(game.key or game.name)
  local lit = hovered == key
  local cells = avgCells(game.seconds, avg.days, {})
  table.insert(cells, 1, ui.label({ text = tostring(game.name), fontSize = 13, flexGrow = 1, maxLines = 1 }))
  return ui.column({
    key = key,
    gap = 4,
    paddingH = 8,
    paddingV = 6,
    radius = 8,
    fill = lit and "surface_variant/0.9" or "surface_variant/0",
    border = lit and "primary/0.85" or "outline/0",
    borderWidth = 1,
    onHover = hover(key),
    onClick = function()
      selectGame(game)
    end,
  }, {
    ui.row({ align = "center", gap = 8 }, cells),
    ui.progress({ progress = (tonumber(avg.total) or 0) > 0 and (tonumber(game.seconds) or 0) / avg.total or 0,
                  height = 5, radius = 2, track = "on_surface/0.22" }),
  })
end

-- One column per weekday: name, average and a bar (see averages.weekdayBar).
local WEEKDAY_BAR = "day"

local function weekdayRow(avg)
  local list = averages.weekdays(avg)
  if #list ~= 7 then
    return nil
  end
  local cells = {}
  for i, w in ipairs(list) do
    local seconds = tonumber(w.average) or 0
    cells[#cells + 1] = ui.column({ key = "avg-wd-" .. i, flexGrow = 1, gap = 4, padding = 8, radius = 10,
                                    fill = "surface_variant/0.9", border = "outline/0.6", borderWidth = 1 }, {
      ui.label({ text = noctalia.tr("ui.wd-" .. i), fontSize = 11, color = "on_surface_variant" }),
      ui.label({ text = shared.duration(seconds), fontSize = 14, fontWeight = "bold", maxLines = 1 }),
      ui.progress({ progress = averages.weekdayBar(list, i, WEEKDAY_BAR), height = 5, radius = 2, track = "on_surface/0.22" }),
    })
  end
  return ui.column({ gap = 6 }, {
    ui.label({ text = noctalia.tr("ui.avg-weekdays"), fontSize = 11, color = "on_surface_variant" }),
    ui.row({ gap = 6 }, cells),
  })
end

local function averagesView(header)
  local avg = data.averages or {}
  local body = { header, tabRow("avg") }
  local since = averages.since(avg)
  if since == nil then
    body[#body + 1] = ui.label({ text = noctalia.tr("ui.avg-none"), color = "on_surface_variant" })
    body[#body + 1] = footer()
    panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1, align = "stretch" }, body))
    return
  end
  local cards = {}
  for _, unit in ipairs(averages.UNITS) do
    cards[#cards + 1] = avgCard(unit, avg)
  end
  body[#body + 1] = ui.row({ gap = 8 }, cards)
  body[#body + 1] = weekdayRow(avg)
  body[#body + 1] = ui.label({ text = noctalia.tr("ui.avg-based", { days = averages.days(avg), since = since }),
                               fontSize = 11, color = "on_surface_variant", maxLines = 2 })
  local headerCells = avgCells(0, avg.days, { header = true })
  table.insert(headerCells, 1, ui.label({ text = noctalia.tr("ui.avg-game"), fontSize = 11,
                                          color = "on_surface_variant", flexGrow = 1 }))
  body[#body + 1] = ui.row({ align = "center", gap = 8, paddingH = 9 }, headerCells)
  local rows = {}
  for i, g in ipairs(avg.games or {}) do
    if i > MAX_GAMES then
      break
    end
    rows[#rows + 1] = avgGameRow(g, avg)
  end
  body[#body + 1] = ui.scroll({ gap = 6, flexGrow = 1 }, rows)
  body[#body + 1] = footer()
  panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1, align = "stretch" }, body))
end

function render()
  if not open then
    return
  end
  local header = ui.row({ align = "center", gap = 8 }, {
    ui.glyph({ name = "device-gamepad-2", size = 20, color = "primary" }),
    ui.label({ text = noctalia.tr("ui.title"), fontSize = 18, fontWeight = "bold" }),
  })

  if data == nil or data.periods == nil then
    panel.render(ui.column({ gap = 12, padding = 16 }, {
      header,
      ui.label({ text = noctalia.tr("ui.no-data"), color = "on_surface_variant" }),
    }))
    return
  end
  if selected then
    gameView(header)
    return
  end

  local tab = currentTab()
  if tab == "avg" then
    averagesView(header)
    return
  end
  local period = data.periods[tab] or { total = 0, games = {}, buckets = {} }
  local total = tonumber(period.total) or 0

  local body = {
    header,
    tabRow(tab),
    ui.row({ align = "center" }, {
      ui.label({ text = noctalia.tr("ui.total"), color = "on_surface_variant", flexGrow = 1 }),
      ui.label({ text = shared.duration(total), fontSize = 18, fontWeight = "bold" }),
    }),
  }
  local tracked = 0
  for _, b in ipairs(period.buckets or {}) do
    tracked = tracked + (tonumber(b.seconds) or 0)
  end
  -- All time without tracked years yet would be one empty bar.
  if tab ~= "all" or tracked > 0 then
    body[#body + 1] = chart(tab, period.buckets or {})
  end
  local before = tonumber(period.before) or 0
  if before > 0 then
    body[#body + 1] = ui.label({ text = noctalia.tr("ui.before", { time = shared.duration(before) }),
                                 fontSize = 11, color = "on_surface_variant" })
  end

  local games = {}
  local list = period.games or {}
  for i, g in ipairs(list) do
    if i > MAX_GAMES then
      games[#games + 1] = ui.label({ text = noctalia.tr("ui.more", { count = #list - MAX_GAMES }),
                                     fontSize = 11, color = "on_surface_variant" })
      break
    end
    games[#games + 1] = gameRow(g, total)
  end
  if #games == 0 then
    games[1] = ui.label({ text = noctalia.tr("ui.nothing"), color = "on_surface_variant" })
  end
  body[#body + 1] = ui.scroll({ gap = 10, flexGrow = 1 }, games)
  body[#body + 1] = footer()

  panel.render(ui.column({ gap = 12, padding = 16, flexGrow = 1, align = "stretch" }, body))
end

local function refresh()
  shared.read(function(d)
    data = d
    render()
  end, data)
  if selected then
    loadHistory()
  end
end

-- `noctalia msg panel-open soulmanyes/playtime:panel all` opens on a tab;
-- `... "game steam:240 day 2026-11-13"` opens a game view (level and date
-- optional), with the key as history.json and summary.json give it.
function onOpen(context)
  for _, name in ipairs(TABS) do
    if context == name then
      noctalia.state.set("tab", name)
    end
  end
  local key, level, y, m, d = tostring(context or ""):match(
    "^game%s+(%S+)%s*(%a*)%s*(%d*)%-?(%d*)%-?(%d*)$")
  if key then
    selectGame({ key = key, name = key })
    if table.find(history.LEVELS, level) then
      view.level = level
    end
    if y ~= "" and m ~= "" and d ~= "" then
      view.y, view.m, view.d = tonumber(y), tonumber(m), tonumber(d)
    end
  end
  open = true
  render()
  refresh()
  -- Re-read while open so a running session keeps counting.
  panel.setWantsSecondTicks(true)
end

local ticks = 0
function update()
  ticks = ticks + 1
  if ticks % 30 == 0 then
    refresh()
  end
end

function onClose()
  open = false
  -- Reopening starts on the list again.
  selected, view = nil, nil
  panel.setWantsSecondTicks(false)
end

function onKey(chord, pressed)
  if not pressed then
    return
  end
  local key = tostring(chord or ""):lower()
  -- In a game view Escape goes up a level and the arrows move the period.
  if selected then
    if key == "escape" then
      goUp()
    elseif key == "left" or key == "right" then
      view = history.shift(view, key == "left" and -1 or 1)
      render()
    end
    return
  end
  if key == "escape" then
    panel.close()
  elseif key == "left" or key == "right" then
    local tab = currentTab()
    local idx = 1
    for i, name in ipairs(TABS) do
      if name == tab then
        idx = i
      end
    end
    idx = key == "left" and ((idx - 2) % #TABS + 1) or (idx % #TABS + 1)
    noctalia.state.set("tab", TABS[idx])
    render()
  end
end
