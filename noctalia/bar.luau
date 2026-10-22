--!nonstrict
-- Playtime bar widget: gamepad glyph plus today's total. The glyph turns
-- primary while a game is running.

local shared = require("./shared.luau")

local POLL_MS = 30000

local data = nil

local function render()
  barWidget.setGlyph("device-gamepad-2")
  if data == nil then
    barWidget.setGlyphColor("on_surface_variant")
    barWidget.setText("")
    barWidget.setTooltip(noctalia.tr("ui.no-data"))
    return
  end

  local today = data.periods and data.periods.day and data.periods.day.total or 0
  local live = data.live or {}
  local playing = #live > 0 and not shared.stale(data)

  barWidget.setGlyphColor(playing and "primary" or "on_surface")
  barWidget.setText(today > 0 and shared.duration(today) or "")

  local lines = { noctalia.tr("ui.today", { time = shared.duration(today) }) }
  if playing then
    for _, s in ipairs(live) do
      local game = s.name
      if s.detail ~= nil and s.detail ~= "" then
        game = game .. " (" .. s.detail .. ")"
      end
      lines[#lines + 1] = noctalia.tr("ui.playing", { game = game, time = shared.duration(s.seconds) })
    end
  end
  if shared.stale(data) then
    lines[#lines + 1] = noctalia.tr("ui.stale", { ago = shared.ago(data.updated) })
  end
  barWidget.setTooltip(table.concat(lines, "\n"))
end

function update()
  noctalia.setUpdateInterval(POLL_MS)
  shared.read(function(d)
    data = d
    render()
  end, data)
end

function onClick()
  noctalia.togglePanel("szblaguen/playtime:panel")
end
