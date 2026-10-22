--!nonstrict
-- Playtime panel: tabs for day/week/month/year/all time, a bar chart of the
-- period's buckets (hours, days, months or years) and per-game totals.

local shared = require("./shared.luau")

local TABS = { "day", "week", "month", "year", "all" }
local CHART_HEIGHT = 72
local CHART_WIDTH = 380
-- Sparse x labels so 24 hours or 31 days stay readable.
local LABEL_EVERY = { day = 6, week = 1, month = 5, year = 1, all = 1 }
-- All time holds every Steam game ever played; a full list is slow to render.
local MAX_GAMES = 50

local data = nil
local open = false

local function currentTab()
  local t = noctalia.state.get("tab")
  for _, name in ipairs(TABS) do
    if name == t then
      return t
    end
  end
  return "week"
end

local function tabRow(selected)
  local tabs = {}
  for _, name in ipairs(TABS) do
    local active = name == selected
    tabs[#tabs + 1] = ui.row({
      key = "tab-" .. name,
      align = "center",
      justify = "center",
      flexGrow = 1,
      minHeight = 30,
      paddingH = 8,
      radius = 8,
      fill = active and "primary/0.14" or "surface_variant/0.22",
      border = active and "primary/0.45" or "outline/0.08",
      borderWidth = 1,
      onClick = function()
        noctalia.state.set("tab", name)
        render()
      end,
    }, {
      ui.label({ text = noctalia.tr("ui.tab-" .. name), fontSize = 12,
                 fontWeight = active and "bold" or "normal",
                 color = active and "on_surface" or "on_surface_variant" }),
    })
  end
  return ui.row({ gap = 4 }, tabs)
end

local function chart(tab, buckets)
  local peak = 1
  for _, b in ipairs(buckets) do
    peak = math.max(peak, tonumber(b.seconds) or 0)
  end
  local n = math.max(1, #buckets)
  local barWidth = math.max(4, math.min(28, math.floor(CHART_WIDTH / n) - 3))
  local every = LABEL_EVERY[tab] or 1
  local cols = {}
  for i, b in ipairs(buckets) do
    local seconds = tonumber(b.seconds) or 0
    local showLabel = (i - 1) % every == 0
    cols[#cols + 1] = ui.column({
      key = "bar-" .. tostring(i),
      gap = 4,
      align = "center",
      flexGrow = 1,
      tooltip = tostring(b.label) .. " · " .. shared.duration(seconds),
    }, {
      ui.spacer({ flexGrow = 1 }),
      ui.box({
        width = barWidth,
        height = seconds > 0 and math.max(3, math.floor(CHART_HEIGHT * seconds / peak)) or 2,
        radius = 3,
        fill = seconds > 0 and "primary" or "outline/0.2",
      }),
      ui.label({ text = showLabel and tostring(b.label) or " ", fontSize = 10,
                 color = "on_surface_variant" }),
    })
  end
  return ui.row({ gap = 2, height = CHART_HEIGHT + 22 }, cols)
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
    ui.progress({ progress = total > 0 and seconds / total or 0, height = 5, radius = 2 }),
  }
  if #details > 0 then
    children[#children + 1] = ui.label({ text = table.concat(details, "   "), fontSize = 11,
                                         color = "on_surface_variant", maxLines = 2 })
  end
  return ui.column({ key = "game-" .. tostring(game.name), gap = 4 }, children)
end

local function footer()
  local sync = data.sync or {}
  local text, color
  if sync.error ~= nil and sync.error ~= "" then
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
  return ui.column({ gap = 2 }, rows)
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

  local tab = currentTab()
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
end

-- `noctalia msg panel-open szblaguen/playtime:panel all` opens on a tab.
function onOpen(context)
  for _, name in ipairs(TABS) do
    if context == name then
      noctalia.state.set("tab", name)
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
  panel.setWantsSecondTicks(false)
end

function onKey(chord, pressed)
  if not pressed then
    return
  end
  local key = tostring(chord or ""):lower()
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
