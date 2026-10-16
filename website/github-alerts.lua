-- Render GitHub alerts (> [!NOTE], > [!TIP], > [!IMPORTANT], > [!WARNING], > [!CAUTION]) as Quarto callouts.
-- Quarto reads Pandoc Markdown, not GFM, so without this filter the marker is printed as literal text.
local TYPES = { NOTE = "note", TIP = "tip", IMPORTANT = "important", WARNING = "warning", CAUTION = "caution" }

function BlockQuote(el)
  local first = el.content[1]
  if not first or first.t ~= "Para" then return nil end
  local marker = pandoc.utils.stringify(first.content[1] or "")
  local kind = TYPES[marker:match("^%[!(%u+)%]$") or ""]
  if not kind then return nil end
  -- Drop the marker and the line break that follows it; keep the rest of the paragraph.
  local rest = pandoc.List({ table.unpack(first.content, 2) })
  while #rest > 0 and (rest[1].t == "SoftBreak" or rest[1].t == "LineBreak" or rest[1].t == "Space") do
    rest:remove(1)
  end
  local body = pandoc.List({ table.unpack(el.content, 2) })
  if #rest > 0 then body:insert(1, pandoc.Para(rest)) end
  return quarto.Callout({ type = kind, content = body })
end
