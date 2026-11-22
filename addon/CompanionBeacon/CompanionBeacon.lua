-- Companion Beacon: your exact position as a strip of coloured squares in the bottom-right corner of the
-- screen, read by the WoW Companion (beacon.py). It reads the game's own position functions and draws;
-- the other things it does: switch the combat and chat logs on at login (owner's choice, 2026-11-18), and
-- inspect your party members one at a time, out of combat and in range, for their gear (2026-11-19).
-- And only if you type /cbeacon rings: hide the quest-area rings on the minimap (owner's choice, 2026-11-21).
--
-- The strip is ROWS rows of CELLS squares of CELL x CELL real screen pixels. Each square carries 6 bits:
-- red, green and blue each at one of 4 levels (0, 1/3, 2/3, 1). Keep this layout in step with beacon.py.
-- Each row has its own checksum, so a row is right or it is not read.
--
-- Row 1: where you are
--   1-4    white, black, 1/3 grey, 2/3 grey   (the reader measures the 4 levels from these)
--   5-7    red, green, blue                   (marks the strip)
--   8      version
--   9      counter (changes on every update)
--   10     flags: 1 position known, 2 indoors, 4 on a flight, 8 in an instance, 16 dead, 32 map rectangle known
--   11     minimap zoom (0-7) + 8 map position known + 16 mounted
--   12-13  instance / continent id
--   14-17  world position, north-south   (24 bits: (yards + 100000) * 50)
--   18-21  world position, west-east
--   22-23  facing (4096 steps round)
--   24-25  zone map id
--   26-27  position on the zone map, across (0-4000)
--   28-29  position on the zone map, down
--   30-45  the zone map's rectangle: north, south, west, east (24 bits each, as the world position)
--   46-47  checksum of squares 8-45
--
-- Row 2: your numbers
--   1      level
--   2-5    XP            6-9   XP for the level       10-13  rested XP left
--   14-16  health        17-19 health max
--   20-22  power (mana)  23-25 power max              26     power type (0 mana, 1 rage, 3 energy)
--   27     flags: 1 in combat, 2 resting (inn / city), 4 rested XP known, 8 a target, 16 target dead, 32 in a group
--   28     players in the group (0 = alone)
--   29     target level (0 none, 63 skull)
--   30     known: 1 XP, 2 health, 4 power, 8 money, 16 bags, 32 durability
--          (off = the game keeps that value secret from add-ons, or has no such function: 0 is drawn)
--   31-35  money in copper
--   36     free bag slots (63 = 63 or more)           37-38  bag slots in all
--   39-40  the most worn piece of gear, in thousandths (1000 = like new)
--   41     quests in the log                          42     party members besides you (0-4)
--   43-44  target health in thousandths + 1 (0 = no target, or the game keeps it secret)      (version 6)
--   45     the target: 1 hostile / 2 neutral / 3 friendly to you (0 not known), + 4 a player,
--          + 8 you can attack it, + 16 someone else's (tapped: no loot or XP for you)
--   46-47  checksum of squares 1-45
--
-- Rows 3-6: texts, a page of 20 bytes at a time, each page held for 4 updates. A text that changed goes next.
--   1      which text (0 = nothing):
--            row 3:    1 the name above the minimap, 2 the zone, 3 the target,
--                      9 where the minimap is: "x y r w h" = its centre and radius in real pixels from the
--                      window's top-left, and the window's size (a character can have its own layout),
--                      10 your character's name
--            row 4:    4-7 party member 1-4: name <tab> level <tab> class
--                      8 your gear, 51-54 party member 1-4's gear ("" until inspected): one entry a slot, in the
--                      order head neck shoulder chest waist legs feet wrist hands ring ring trinket trinket
--                      back main-hand off-hand ranged, a space between: itemlevel.quality (t after it = two-handed),
--                      0 = empty slot, ? = the game has not told us about that item yet
--            rows 5-6: 10 + n = quest n of the log (odd n on row 5, even on row 6):
--                      level <tab> flags (1 complete, 2 failed) <tab> title <tab> objective <tab> objective ...
--                      (an objective starts with + when it is finished, - when not)
--            row 7 (version 8):  55 your buffs, 56 your debuffs: a line each, name <tab> when it ends (the PC
--                      clock's seconds, 0 = it does not run out) <tab> stacks
--                      57 spells on your bars that are cooling down: name <tab> when ready (PC clock's seconds)
--                      58 what you carry of food, drink, potions, reagents, ammunition and soul shards:
--                      name <tab> how many <tab> item class.subclass
--            row 8 (version 8):  59 the last 6 things that happened: number <tab> kind <tab> who / what
--                      (vendor, repair, trainer, flight, mail, bank, inn, whisper, ready, level, dead, gather)
--                      60 the quests the game marks on this zone's map: the map id on the first line, then
--                      title <tab> across <tab> down (thousandths of the zone map)
--                      61 s <tab> skill <tab> rank <tab> most, and t <tab> talent tree <tab> points
--                      62 reputations: name <tab> standing (1-8) <tab> points into it <tab> points it takes
--                      63 rings <tab> 1 shown / 0 hidden by /cbeacon rings / x the game has no such switch
--   2      counter, changes when that text changes
--   3      the text's length in bytes, low 6 bits (square 45 has the rest; cut at 1200)
--   4      page number (the page starts at 20 x this)
--   5-44   20 bytes, two squares each (byte / 64, byte % 64); 0 past the end
--   45     length, high 6 bits
--   46-47  checksum of squares 1-45
--
-- Above the strip (version 7): BARS bars, each as wide as the strip and one square high, that the GAME fills.
-- WoW Forever keeps health and power secret from add-ons as numbers, but a bar may be handed a secret value
-- and draws it; the companion measures how much of each bar is lit (green on black).
--   bar 1 (the row just above the strip)  your health
--   bar 2                                 your power (mana, rage, energy)
--   bar 3                                 the target's health (row 2 says whether there is a target)
--   bars 4-7 (version 8)                  party member 1-4's health (texts 4-7 say whether there is one)
-- A bar with a blue background = the game refused even that: the companion reads the screen as before.

local CELL, CELLS, ROWS, VERSION = 4, 47, 8, 8
local BARS = 7
local PAGE, HOLD, TEXT_MAX, QUEST_MAX = 20, 4, 1200, 40
local floor = math.floor

local f = CreateFrame("Frame", "CompanionBeaconFrame")      -- no parent: hiding the UI does not hide it
f:SetFrameStrata("TOOLTIP")
f:SetSize(CELL * CELLS, CELL * ROWS)
f:SetPoint("BOTTOMRIGHT", WorldFrame, "BOTTOMRIGHT", 0, 0)   -- (owner's choice 2026-11-19; it was top-left before)

local tex, shown = {}, {}
for i = 1, CELLS * ROWS do                                  -- square i: row 1 is 1-47, row 2 is 48-94, ...
    local t = f:CreateTexture(nil, "OVERLAY")
    t:SetSize(CELL, CELL)
    t:SetPoint("TOPLEFT", f, "TOPLEFT", ((i - 1) % CELLS) * CELL, -floor((i - 1) / CELLS) * CELL)
    t:SetColorTexture(0, 0, 0, 1)
    tex[i] = t
end

local bars = {}
for i = 1, BARS do
    local b = CreateFrame("StatusBar", nil, f)
    b:SetSize(CELL * CELLS, CELL)
    b:SetPoint("BOTTOMLEFT", f, "TOPLEFT", 0, (i - 1) * CELL)
    b:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8")
    b:SetStatusBarColor(0, 1, 0, 1)
    b:SetMinMaxValues(0, 1)
    b:SetValue(0)
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetAllPoints(b)
    bg:SetColorTexture(0, 0, 0, 1)
    b.bg = bg
    bars[i] = b
end

local function fill(i, unit, cur, max)                      -- bar i = cur(unit) of max(unit); either may be secret
    local b = bars[i]
    local ok = pcall(function()
        b:SetMinMaxValues(0, max(unit))
        b:SetValue(cur(unit))
    end)
    if b.ok ~= ok then
        b.ok = ok
        if ok then
            b.bg:SetColorTexture(0, 0, 0, 1)
        else
            b.bg:SetColorTexture(0, 0, 1, 1)                -- refused: say so in blue, with nothing lit
            pcall(function()
                b:SetMinMaxValues(0, 1)
                b:SetValue(0)
            end)
        end
    end
end

local function rescale()                                    -- one unit = one real screen pixel
    local _, h = GetPhysicalScreenSize()
    if h and h > 0 then
        f:SetScale(768 / h)
    end
end

local function set(i, v)                                    -- square i shows the 6-bit value v
    if shown[i] ~= v then
        shown[i] = v
        tex[i]:SetColorTexture((floor(v / 16) % 4) / 3, (floor(v / 4) % 4) / 3, (v % 4) / 3, 1)
    end
end

local function put(i, n, v)                                 -- n squares from i: v, highest bits first
    for k = n - 1, 0, -1 do
        set(i + (n - 1 - k), floor(v / 64 ^ k) % 64)
    end
end

local function yards(c)
    local v = floor((c + 100000) * 50 + 0.5)
    if v < 0 then v = 0 elseif v > 16777215 then v = 16777215 end
    return v
end

local rects = {}
local function rect(map)                                    -- the zone map's corners in world yards
    local r = rects[map]
    if r == nil then
        r = false
        pcall(function()
            local _, tl = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(0, 0))
            local _, br = C_Map.GetWorldPosFromMapPos(map, CreateVector2D(1, 1))
            local n, w = tl:GetXY()
            local s, e = br:GetXY()
            if n and s and w and e and n ~= s and w ~= e then
                r = { n, s, w, e }
            end
        end)
        rects[map] = r
    end
    return r
end

local function clamp(v, top)
    v = floor(tonumber(v) or 0)
    if v < 0 then return 0 elseif v > top then return top end
    return v
end

local function seal(row)                                    -- a row's checksum (rows 2 and up)
    local base, sum = (row - 1) * CELLS, 1000 * row
    for k = 1, 45 do
        sum = (sum + (shown[base + k] or 0) * k) % 4096
    end
    put(base + 46, 2, sum)
end

-- The game hands add-ons some values as "secret" (WoW Forever: your health and power): they can be shown
-- by the game's own bars but not counted with, and touching one is an error. Such a value is left out
-- (its "known" flag stays off) and the companion reads that bar off the screen as before.
local function num(fn, ...)                                 -- fn(...) as a plain whole number, or nil
    local ok, v = pcall(fn, ...)
    if not ok or (issecretvalue and issecretvalue(v)) then return nil end
    local ok2, n = pcall(function() return floor(tonumber(v)) end)   -- (nil or not a number: an error, so nil)
    if ok2 and n then return n end
    return nil
end

local function yes(fn, ...)                                 -- fn(...) is true (and not secret)
    if not fn then return false end
    local ok, v = pcall(fn, ...)
    if not ok or (issecretvalue and issecretvalue(v)) then return false end
    local ok2, t = pcall(function() if v then return true end return false end)
    return ok2 and t
end

local slow = {}                                             -- looked up once a second (see survey)

local function numbers()                                    -- row 2
    local B = CELLS
    local flags, known = 0, 0
    local level = num(UnitLevel, "player")
    local xp, xpmax = num(UnitXP, "player"), num(UnitXPMax, "player")
    local rested = GetXPExhaustion and num(GetXPExhaustion)
    local hp, hpmax = num(UnitHealth, "player"), num(UnitHealthMax, "player")
    local pw, pwmax = num(UnitPower, "player"), num(UnitPowerMax, "player")
    local ptype = num(UnitPowerType, "player")
    if yes(UnitAffectingCombat, "player") then flags = flags + 1 end
    if yes(IsResting) then flags = flags + 2 end
    if rested then flags = flags + 4 end
    local tlevel, thp, tinfo = 0, 0, 0
    if yes(UnitExists, "target") then
        flags = flags + 8
        local tl = num(UnitLevel, "target")
        if tl then
            if tl < 0 or tl > 62 then tlevel = 63 else tlevel = tl end
        end
        if yes(UnitIsDead, "target") then flags = flags + 16 end
        local th, thmax = num(UnitHealth, "target"), num(UnitHealthMax, "target")
        if th and thmax and thmax > 0 then thp = clamp(th * 1000 / thmax + 0.5, 1000) + 1 end
        local r = num(UnitReaction, "target", "player")     -- 1-3 hostile, 4 neutral, 5-8 friendly
        if r then
            if r <= 3 then tinfo = 1 elseif r == 4 then tinfo = 2 else tinfo = 3 end
        end
        if yes(UnitIsPlayer, "target") then tinfo = tinfo + 4 end
        if yes(UnitCanAttack, "player", "target") then tinfo = tinfo + 8 end
        if yes(UnitIsTapDenied, "target") then tinfo = tinfo + 16 end
    end
    local group = GetNumGroupMembers and num(GetNumGroupMembers) or 0
    if group > 0 then flags = flags + 32 end
    if xp and xpmax and xpmax > 0 then known = known + 1 end
    if hp and hpmax and hpmax > 0 then known = known + 2 end
    if pw and pwmax and ptype then known = known + 4 end
    set(B + 1, clamp(level, 63))
    put(B + 2, 4, clamp(xp, 16777215))
    put(B + 6, 4, clamp(xpmax, 16777215))
    put(B + 10, 4, clamp(rested, 16777215))
    put(B + 14, 3, clamp(hp, 262143))
    put(B + 17, 3, clamp(hpmax, 262143))
    put(B + 20, 3, clamp(pw, 262143))
    put(B + 23, 3, clamp(pwmax, 262143))
    set(B + 26, clamp(ptype, 63))
    set(B + 27, flags)
    set(B + 28, clamp(group, 63))
    set(B + 29, tlevel)
    if slow.money then known = known + 8 end
    if slow.bag_all then known = known + 16 end
    if slow.wear then known = known + 32 end
    set(B + 30, known)
    put(B + 31, 5, clamp(slow.money, 1073741823))
    set(B + 36, clamp(slow.bag_free, 63))
    put(B + 37, 2, clamp(slow.bag_all, 4095))
    put(B + 39, 2, clamp(slow.wear, 1000))
    set(B + 41, clamp(slow.quests, 63))
    set(B + 42, clamp(slow.party, 4))
    put(B + 43, 2, thp)
    set(B + 45, tinfo)
    seal(2)
end

local function text(fn, ...)                                -- fn(...) as a plain string ("" if none or secret)
    if not fn then return "" end
    local ok, v = pcall(fn, ...)
    if not ok or (issecretvalue and issecretvalue(v)) or type(v) ~= "string" then return "" end
    local ok2, n = pcall(function() return #v + v:byte(1, 1) * 0 end)   -- (it can be measured and read)
    if ok2 and n then return v end
    return ""
end

-- ---- texts (rows 3-6)
local strings, sids, dirty = {}, {}, {}                     -- text number -> the text, its counter, "changed"
local function say(chan, s)
    if #s > TEXT_MAX then s = s:sub(1, TEXT_MAX) end
    if strings[chan] ~= s then
        strings[chan] = s
        sids[chan] = ((sids[chan] or 0) + 1) % 64
        dirty[chan] = true
    end
end

local trows = {                                             -- one per text row: its texts, and where it is
    { row = 3, chans = { 1, 2, 3, 9, 10 }, at = 0, page = 0, held = 0 },
    { row = 4, chans = { 4, 5, 6, 7, 8, 51, 52, 53, 54 }, at = 0, page = 0, held = 0 },
    { row = 5, chans = {}, at = 0, page = 0, held = 0 },
    { row = 6, chans = {}, at = 0, page = 0, held = 0 },
    { row = 7, chans = { 55, 56, 57, 58 }, at = 0, page = 0, held = 0 },
    { row = 8, chans = { 59, 60, 61, 62, 63 }, at = 0, page = 0, held = 0 },
}
for c = 1, 10 do say(c, "") end
for c = 55, 63 do say(c, "") end
for c = 51, 54 do say(c, "") end

local function turn(tr)                                     -- which text and page this row shows now
    local n = #tr.chans
    if n == 0 then
        tr.chan = nil
        return
    end
    local chan = tr.chan
    if chan and tr.sid ~= sids[chan] then                   -- it changed while being sent: from its start again
        tr.page, tr.held, tr.sid = 0, 1, sids[chan]
        dirty[chan] = nil
        return
    end
    tr.held = tr.held + 1
    if chan and tr.held <= HOLD then return end
    tr.held = 1
    if chan and (tr.page + 1) * PAGE < #strings[chan] then
        tr.page = tr.page + 1
        return
    end
    local nxt
    for i = 1, n do
        if dirty[tr.chans[i]] then nxt = i break end
    end
    if not nxt then nxt = tr.at % n + 1 end
    tr.at = nxt
    tr.chan = tr.chans[nxt]
    tr.page, tr.sid = 0, sids[tr.chan]
    dirty[tr.chan] = nil
end

local function show(tr)
    local B = (tr.row - 1) * CELLS
    local chan = tr.chan
    local s = chan and strings[chan] or ""
    local off = tr.page * PAGE
    set(B + 1, chan or 0)
    set(B + 2, chan and sids[chan] or 0)
    set(B + 3, #s % 64)
    set(B + 4, chan and tr.page or 0)
    for k = 0, PAGE - 1 do
        local b = s:byte(off + k + 1) or 0
        set(B + 5 + 2 * k, floor(b / 64))
        set(B + 6 + 2 * k, b % 64)
    end
    set(B + 45, floor(#s / 64))
    seal(tr.row)
end

local function pages()                                      -- rows 3-6
    say(1, text(GetMinimapZoneText))
    say(2, text(GetRealZoneText))
    say(3, text(UnitName, "target"))
    say(10, text(UnitName, "player"))
    for i = 1, #trows do
        turn(trows[i])
        show(trows[i])
    end
end

-- ---- once a second: party, quests, money, bags, gear (each on its own: one that fails leaves the others)
local function party()
    local n = 0
    for i = 1, 4 do
        local unit = "party" .. i
        local s = ""
        if yes(UnitExists, unit) then
            local name = text(UnitName, unit)
            if name ~= "" then
                n = n + 1
                local class = ""
                local ok, _, token = pcall(UnitClass, unit)
                if ok and type(token) == "string" and not (issecretvalue and issecretvalue(token)) then class = token end
                s = name .. "\t" .. (num(UnitLevel, unit) or 0) .. "\t" .. class
            end
        end
        say(3 + i, s)
    end
    slow.party = n
end

local function clean(v)                                     -- a text for a quest line: plain, no tabs
    if type(v) ~= "string" or (issecretvalue and issecretvalue(v)) then return "" end
    return (v:gsub("[%c]", " "))
end

local function quests()
    local list = {}
    if C_QuestLog and C_QuestLog.GetInfo and C_QuestLog.GetNumQuestLogEntries then      -- the newer functions
        local n = C_QuestLog.GetNumQuestLogEntries() or 0
        for i = 1, n do
            local q = C_QuestLog.GetInfo(i)
            if q and not q.isHeader and not q.isHidden and q.title and #list < QUEST_MAX then
                local flags = 0
                if C_QuestLog.IsComplete and q.questID and C_QuestLog.IsComplete(q.questID) then flags = 1 end
                if C_QuestLog.IsFailed and q.questID and C_QuestLog.IsFailed(q.questID) then flags = 2 end
                local line = (tonumber(q.level) or 0) .. "\t" .. flags .. "\t" .. clean(q.title)
                local objs = q.questID and C_QuestLog.GetQuestObjectives and C_QuestLog.GetQuestObjectives(q.questID)
                if type(objs) == "table" then
                    for _, o in ipairs(objs) do
                        if o and o.text and o.text ~= "" then
                            line = line .. "\t" .. (o.finished and "+" or "-") .. clean(o.text)
                        end
                    end
                end
                list[#list + 1] = line
            end
        end
    elseif GetNumQuestLogEntries and GetQuestLogTitle then                              -- the Classic ones
        local n = GetNumQuestLogEntries() or 0
        for i = 1, n do
            local title, level, _, isHeader, _, isComplete = GetQuestLogTitle(i)
            if title and not isHeader and #list < QUEST_MAX then
                local flags = 0
                if isComplete == 1 or isComplete == true then flags = 1 elseif isComplete == -1 then flags = 2 end
                local line = (tonumber(level) or 0) .. "\t" .. flags .. "\t" .. clean(title)
                for j = 1, (GetNumQuestLeaderBoards and GetNumQuestLeaderBoards(i) or 0) do
                    local desc, _, done = GetQuestLogLeaderBoard(j, i)
                    if desc and desc ~= "" then
                        line = line .. "\t" .. (done and "+" or "-") .. clean(desc)
                    end
                end
                list[#list + 1] = line
            end
        end
    else
        return
    end
    for i = 1, #list do say(10 + i, list[i]) end
    if slow.quests ~= #list then                            -- the rows' lists: odd quests on row 5, even on row 6
        local odd, even = {}, {}
        for i = 1, #list do
            if i % 2 == 1 then odd[#odd + 1] = 10 + i else even[#even + 1] = 10 + i end
        end
        trows[3].chans, trows[3].chan, trows[3].at = odd, nil, 0
        trows[4].chans, trows[4].chan, trows[4].at = even, nil, 0
        slow.quests = #list
    end
end

local function purse()
    slow.money = num(GetMoney)
    local free_fn = (C_Container and C_Container.GetContainerNumFreeSlots) or GetContainerNumFreeSlots
    local all_fn = (C_Container and C_Container.GetContainerNumSlots) or GetContainerNumSlots
    local free, all = 0, 0
    if free_fn and all_fn then
        for bag = 0, 4 do
            free = free + (num(free_fn, bag) or 0)
            all = all + (num(all_fn, bag) or 0)
        end
        slow.bag_free, slow.bag_all = free, all > 0 and all or nil
    end
    local worst
    if GetInventoryItemDurability then
        for slot = 1, 19 do
            local ok, cur, top = pcall(GetInventoryItemDurability, slot)
            if ok and not (issecretvalue and (issecretvalue(cur) or issecretvalue(top))) and cur and top and top > 0 then
                local w = floor(cur / top * 1000 + 0.5)
                if not worst or w < worst then worst = w end
            end
        end
    end
    slow.wear = worst
end

-- ---- gear: yours every second; a party member's when the game answers an inspect (see "asking")
local GEAR_SLOTS = { 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18 }
local function gear_of(unit)                                -- the text for that unit's gear, and "an item is not known yet"
    local out, missing = {}, false
    local info = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    local detail = (C_Item and C_Item.GetDetailedItemLevelInfo) or GetDetailedItemLevelInfo
    for k = 1, #GEAR_SLOTS do
        local entry = "0"
        local ok, link = pcall(GetInventoryItemLink, unit, GEAR_SLOTS[k])
        if ok and not (issecretvalue and issecretvalue(link)) and type(link) == "string" then
            local ilvl, quality, two
            if info then
                local ok2, _, _, q, lvl, _, _, _, _, loc = pcall(info, link)
                if ok2 then
                    if type(q) == "number" then quality = q end
                    if type(lvl) == "number" then ilvl = lvl end
                    if loc == "INVTYPE_2HWEAPON" then two = true end
                end
            end
            if detail then
                local ok3, v = pcall(detail, link)
                if ok3 and type(v) == "number" and v > 0 then ilvl = v end
            end
            if ilvl and ilvl > 0 then
                entry = floor(ilvl) .. "." .. (quality or 1) .. (two and "t" or "")
            else
                entry, missing = "?", true
            end
        end
        out[k] = entry
    end
    return table.concat(out, " "), missing
end

local seen = {}                                             -- player guid -> { s = gear text, t = when, whole = all items known }
local asking                                                -- the inspect we are waiting for: { guid, unit, t }
local function gear()
    local mine = gear_of("player")
    say(8, mine)
    local now = GetTime()
    for i = 1, 4 do                                         -- the rows show what is known of whoever is in that place now
        local unit = "party" .. i
        local guid = yes(UnitExists, unit) and text(UnitGUID, unit) or ""
        say(50 + i, seen[guid] and seen[guid].s or "")
    end
    if asking and now - asking.t < 4 then return end        -- one question at a time; 4 s for the answer
    asking = nil
    if (InCombatLockdown and InCombatLockdown()) or yes(UnitAffectingCombat, "player") then return end
    if InspectFrame and InspectFrame:IsShown() then return end     -- you are inspecting somebody yourself
    if not (NotifyInspect and CanInspect) then return end
    local best, best_t
    for i = 1, 4 do
        local unit = "party" .. i
        if yes(UnitExists, unit) and yes(UnitIsConnected, unit) and yes(CanInspect, unit) and yes(CheckInteractDistance, unit, 1) then
            local guid = text(UnitGUID, unit)
            local g = seen[guid]
            local last = g and g.t or 0
            -- never asked; or an item was missing (again after 5 s); or it is 2 minutes old
            if guid ~= "" and (not g or (not g.whole and now - last > 5) or now - last > 120) and (not best or last < best_t) then
                best, best_t = { guid = guid, unit = unit, t = now }, last
            end
        end
    end
    if best then
        asking = best
        NotifyInspect(best.unit)
    end
end

local function inspected(guid)                              -- the game answered an inspect
    if not asking or guid ~= asking.guid then return end
    local a = asking
    asking = nil
    if text(UnitGUID, a.unit) == guid then
        local s, missing = gear_of(a.unit)
        seen[guid] = { s = s, t = GetTime(), whole = not missing }
    end
    if ClearInspectPlayer and not (InspectFrame and InspectFrame:IsShown()) then pcall(ClearInspectPlayer) end
end

-- ---- where the minimap is on the screen (text 9). A frame's own units times its scale are 1/768 of the
-- window's height each; the game counts up from the bottom, the companion down from the top.
local function minimap_spot()
    local s = ""
    pcall(function()
        local x, y = Minimap:GetCenter()
        local w, sc = Minimap:GetWidth(), Minimap:GetEffectiveScale()
        local pw, ph = GetPhysicalScreenSize()
        if issecretvalue and (issecretvalue(x) or issecretvalue(y) or issecretvalue(w) or issecretvalue(sc)) then return end
        if x and y and w and sc and pw and ph and ph > 0 and w > 0 then
            local k = sc * ph / 768
            s = string.format("%.1f %.1f %.1f %d %d", x * k, ph - y * k, w * k / 2, pw, ph)
        end
    end)
    say(9, s)
end

-- ---- version 8: buffs, cooldowns, what you carry, what happened, quest places, skills, reputation
local function secret(v) return issecretvalue and issecretvalue(v) and true or false end
local function plain(v)                                     -- v, or nil when the game keeps it secret
    if secret(v) then return nil end
    return v
end

-- A time that runs out is sent as the PC clock's second it ends at, and kept while it stays within 3 s:
-- a text that changed every second would be sent over and over and hold the others up.
local held = {}
local function ends_at(key, left)
    if type(left) ~= "number" or left <= 0 then return 0 end
    local e = floor(time() + left + 0.5)
    local h = held[key]
    if h and math.abs(h - e) <= 3 then return h end
    held[key] = e
    return e
end

local function auras_of(filter)                             -- the list as text, or nil when the game hid part of it
    local out, now, hidden = {}, GetTime(), false
    for i = 1, 40 do
        local name, exp, stacks
        if C_UnitAuras and C_UnitAuras.GetAuraDataByIndex then
            local ok, a = pcall(C_UnitAuras.GetAuraDataByIndex, "player", i, filter)
            if not ok then break end
            if not secret(a) then
                if a == nil then break end
                pcall(function() name, exp, stacks = plain(a.name), plain(a.expirationTime), plain(a.applications) end)
                if type(name) ~= "string" then hidden = true end
            else
                hidden = true
            end
        elseif UnitAura then
            local ok, n, _, c, _, _, e = pcall(UnitAura, "player", i, filter)
            if not ok then break end
            if not secret(n) then
                if n == nil then break end
                name, exp, stacks = n, plain(e), plain(c)
            else
                hidden = true
            end
        else
            break
        end
        if type(name) == "string" and name ~= "" then
            local left = type(exp) == "number" and exp > 0 and (exp - now) or nil
            out[#out + 1] = clean(name) .. "\t" .. ends_at(filter .. name, left) .. "\t" .. (type(stacks) == "number" and stacks or 0)
        end
    end
    if hidden then return nil end
    return table.concat(out, "\n")
end

local function auras()                                      -- (a list the game hid part of - in a fight, maybe -
    local b, d = auras_of("HELPFUL"), auras_of("HARMFUL")   --  is not sent: the last whole one stays)
    if b then say(55, b) end
    if d then say(56, d) end
end

local function spell_name(id)
    local s = text(C_Spell and C_Spell.GetSpellName, id)
    if s == "" then s = text(GetSpellInfo, id) end
    return s
end

local function cooldowns()
    local out, had, now = {}, {}, GetTime()
    if not GetActionInfo then return end
    for slot = 1, 120 do
        local ok, kind, id = pcall(GetActionInfo, slot)
        if ok and plain(kind) == "spell" and type(plain(id)) == "number" then
            local start, dur
            if C_Spell and C_Spell.GetSpellCooldown then
                local ok2, cd = pcall(C_Spell.GetSpellCooldown, id)
                if ok2 and cd and not secret(cd) then
                    pcall(function() start, dur = plain(cd.startTime), plain(cd.duration) end)
                end
            elseif GetSpellCooldown then
                local ok2, s, d = pcall(GetSpellCooldown, id)
                if ok2 then start, dur = plain(s), plain(d) end
            end
            if type(start) == "number" and type(dur) == "number" and dur > 2 then     -- (2 s: not the global one)
                local name = spell_name(id)
                if name ~= "" and not had[name] and #out < 30 then
                    had[name] = true
                    out[#out + 1] = clean(name) .. "\t" .. ends_at("cd" .. name, start + dur - now)
                end
            end
        end
    end
    say(57, table.concat(out, "\n"))
end

local function supplies()
    local slots = (C_Container and C_Container.GetContainerNumSlots) or GetContainerNumSlots
    local info = C_Container and C_Container.GetContainerItemInfo
    if not slots then return end
    local count, order = {}, {}
    for bag = 0, 4 do
        for slot = 1, (num(slots, bag) or 0) do
            local id, n
            if info then
                local ok, it = pcall(info, bag, slot)
                if ok and it and not secret(it) then
                    pcall(function() id, n = plain(it.itemID), plain(it.stackCount) end)
                end
            elseif GetContainerItemInfo then
                local ok, _, c, _, _, _, _, _, _, _, iid = pcall(GetContainerItemInfo, bag, slot)
                if ok then id, n = plain(iid), plain(c) end
            end
            if type(id) == "number" then
                if not count[id] then order[#order + 1] = id end
                count[id] = count[id] or 0
                count[id] = count[id] + (type(n) == "number" and n or 1)
            end
        end
    end
    table.sort(order)
    local instant = (C_Item and C_Item.GetItemInfoInstant) or GetItemInfoInstant
    local nameof = (C_Item and C_Item.GetItemNameByID) or GetItemInfo
    local out = {}
    for _, id in ipairs(order) do
        local class, sub
        if instant then
            local ok, _, _, _, _, _, c, s = pcall(instant, id)
            if ok then class, sub = plain(c), plain(s) end
        end
        -- consumables (0), reagents (5), ammunition (6), and the warlock's soul shards
        if id == 6265 or class == 0 or class == 5 or class == 6 then
            local name = text(nameof, id)
            if name ~= "" and #out < 30 then
                out[#out + 1] = clean(name) .. "\t" .. count[id] .. "\t" .. (type(class) == "number" and class or 0)
                    .. "." .. (type(sub) == "number" and sub or 0)
            end
        end
    end
    say(58, table.concat(out, "\n"))
end

local events, evn = {}, (time and time() or 0) % 1000
local function note(kind, who)                              -- something happened: the last 6 are text 59
    evn = (evn + 1) % 1000
    events[#events + 1] = evn .. "\t" .. kind .. "\t" .. clean(who)
    while #events > 6 do table.remove(events, 1) end
    say(59, table.concat(events, "\n"))
end

local function quest_spots()
    if not (C_Map and C_Map.GetBestMapForUnit) then return end
    local okm, map = pcall(C_Map.GetBestMapForUnit, "player")
    if not okm or type(plain(map)) ~= "number" then
        say(60, "")
        return
    end
    local out = {}
    if C_QuestLog and C_QuestLog.GetQuestsOnMap then
        local ok, list = pcall(C_QuestLog.GetQuestsOnMap, map)
        if ok and not secret(list) and type(list) == "table" then
            for _, q in ipairs(list) do
                pcall(function()
                    local id, x, y = plain(q.questID), plain(q.x), plain(q.y)
                    local title = type(id) == "number" and text(C_QuestLog.GetTitleForQuestID, id) or ""
                    if title ~= "" and type(x) == "number" and type(y) == "number" and #out < 30 then
                        out[#out + 1] = clean(title) .. "\t" .. floor(x * 1000 + 0.5) .. "\t" .. floor(y * 1000 + 0.5)
                    end
                end)
            end
        end
    end
    say(60, map .. "\n" .. table.concat(out, "\n"))
end

local function skills()
    local out = {}
    if GetProfessions and GetProfessionInfo then
        local list = { pcall(GetProfessions) }
        for i = 2, 6 do
            local idx = plain(list[i])
            if list[1] and idx then
                local ok, name, _, rank, top = pcall(GetProfessionInfo, idx)
                if ok and type(plain(name)) == "string" then
                    out[#out + 1] = "s\t" .. clean(name) .. "\t" .. (tonumber(plain(rank)) or 0) .. "\t" .. (tonumber(plain(top)) or 0)
                end
            end
        end
    elseif GetNumSkillLines and GetSkillLineInfo then
        for i = 1, (num(GetNumSkillLines) or 0) do
            local ok, name, header, _, rank, _, _, top = pcall(GetSkillLineInfo, i)
            if ok and type(plain(name)) == "string" and not plain(header) and #out < 40 then
                out[#out + 1] = "s\t" .. clean(name) .. "\t" .. (tonumber(plain(rank)) or 0) .. "\t" .. (tonumber(plain(top)) or 0)
            end
        end
    end
    if GetNumTalentTabs and GetTalentTabInfo then
        for i = 1, (num(GetNumTalentTabs) or 0) do
            local r = { pcall(GetTalentTabInfo, i) }        -- name, picture, points - or id, name, text, picture, points
            if r[1] then
                local name, pts = plain(r[2]), plain(r[4])
                if type(name) ~= "string" then name, pts = plain(r[3]), plain(r[6]) end
                if type(name) == "string" then
                    out[#out + 1] = "t\t" .. clean(name) .. "\t" .. (tonumber(pts) or 0)
                end
            end
        end
    end
    say(61, table.concat(out, "\n"))
end

local function reputation()
    local out = {}
    if C_Reputation and C_Reputation.GetNumFactions and C_Reputation.GetFactionDataByIndex then
        for i = 1, (num(C_Reputation.GetNumFactions) or 0) do
            local ok, d = pcall(C_Reputation.GetFactionDataByIndex, i)
            if ok and d and not secret(d) then
                pcall(function()
                    local name, st, cur, lo, hi = plain(d.name), plain(d.reaction), plain(d.currentStanding),
                        plain(d.currentReactionThreshold), plain(d.nextReactionThreshold)
                    if type(name) == "string" and type(st) == "number" and not (d.isHeader and not d.isHeaderWithRep) and #out < 40 then
                        out[#out + 1] = clean(name) .. "\t" .. st .. "\t" .. ((cur or 0) - (lo or 0)) .. "\t" .. ((hi or 0) - (lo or 0))
                    end
                end)
            end
        end
    elseif GetNumFactions and GetFactionInfo then
        for i = 1, (num(GetNumFactions) or 0) do
            local ok, name, _, st, lo, hi, cur, _, _, header, _, hasrep = pcall(GetFactionInfo, i)
            if ok and type(plain(name)) == "string" and type(plain(st)) == "number" and (not plain(header) or plain(hasrep)) and #out < 40 then
                lo, hi, cur = tonumber(plain(lo)) or 0, tonumber(plain(hi)) or 0, tonumber(plain(cur)) or 0
                out[#out + 1] = clean(name) .. "\t" .. st .. "\t" .. (cur - lo) .. "\t" .. (hi - lo)
            end
        end
    end
    say(62, table.concat(out, "\n"))
end

-- The quest-area rings on the minimap get into the companion's map pictures. /cbeacon rings hides them (and
-- shows them again after a /reload): the one thing here that changes how your game looks, so it is off
-- until you ask.
local RING_FNS = { "SetQuestBlobRingAlpha", "SetQuestBlobInsideAlpha", "SetQuestBlobOutsideAlpha",
                   "SetTaskBlobRingAlpha", "SetTaskBlobInsideAlpha", "SetTaskBlobOutsideAlpha" }
local function rings()
    local hide = type(CompanionBeaconDB) == "table" and CompanionBeaconDB.hide_rings
    local can = false
    for _, fn in ipairs(RING_FNS) do
        if Minimap and Minimap[fn] then
            can = true
            if hide then pcall(Minimap[fn], Minimap, 0) end
        end
    end
    say(63, "rings\t" .. ((not can and "x") or (hide and "0") or "1"))
    return can
end

local surveys = 0
local function survey()
    local e
    surveys = surveys + 1
    local todo = { party, quests, purse, gear, minimap_spot, auras, cooldowns, quest_spots }
    if surveys % 5 == 1 then todo[#todo + 1] = supplies end
    if surveys % 10 == 2 then
        todo[#todo + 1] = skills
        todo[#todo + 1] = reputation
        todo[#todo + 1] = rings
    end
    for _, fn in ipairs(todo) do
        local ok, err = pcall(fn)
        if not ok then e = e or err end
    end
    if e then error(e, 0) end
end

local seq, surveyed = 0, 0
local function update()
    seq = (seq + 1) % 64
    local slow_ok, slow_err = true, nil
    if GetTime and GetTime() - surveyed >= 1 then
        surveyed = GetTime()
        slow_ok, slow_err = pcall(survey)
    end
    local flags, zoom = 0, 0
    local ns, we, inst, facing, map, mx, my = 0, 0, 0, 0, 0, 0, 0
    local r

    local ok, a, b, _, id = pcall(UnitPosition, "player")
    if ok and a and b then
        flags = flags + 1
        ns, we = yards(a), yards(b)
    end
    if ok and id and id >= 0 and id < 4096 then inst = id end
    if not (ok and id) then
        local ok2, _, _, _, _, _, _, _, id2 = pcall(GetInstanceInfo)
        if ok2 and id2 and id2 >= 0 and id2 < 4096 then inst = id2 end
    end
    if IsIndoors and IsIndoors() then flags = flags + 2 end
    if UnitOnTaxi and UnitOnTaxi("player") then flags = flags + 4 end
    if IsInInstance and IsInInstance() then flags = flags + 8 end
    if UnitIsDeadOrGhost and UnitIsDeadOrGhost("player") then flags = flags + 16 end

    local okf, fa = pcall(GetPlayerFacing)
    if okf and fa then facing = floor(fa / (2 * math.pi) * 4096) % 4096 end

    if Minimap and Minimap.GetZoom then
        local okz, z = pcall(Minimap.GetZoom, Minimap)
        if okz and z then zoom = math.min(7, math.max(0, floor(z))) end
    end
    if IsMounted and IsMounted() then zoom = zoom + 16 end

    if C_Map and C_Map.GetBestMapForUnit then
        local okm, m = pcall(C_Map.GetBestMapForUnit, "player")
        if okm and m and m > 0 and m < 4096 then
            map = m
            local okp, p = pcall(C_Map.GetPlayerMapPosition, m, "player")
            if okp and p then
                local x, y = p:GetXY()
                if x and y and x >= 0 and x <= 1 and y >= 0 and y <= 1 then
                    mx, my = floor(x * 4000 + 0.5), floor(y * 4000 + 0.5)
                    zoom = zoom + 8
                end
            end
            r = rect(m)
            if r then flags = flags + 32 end
        end
    end

    set(8, VERSION)
    set(9, seq)
    set(10, flags)
    set(11, zoom)
    put(12, 2, inst)
    put(14, 4, ns)
    put(18, 4, we)
    put(22, 2, facing)
    put(24, 2, map)
    put(26, 2, mx)
    put(28, 2, my)
    for k = 1, 4 do
        put(30 + (k - 1) * 4, 4, r and yards(r[k]) or 0)
    end
    local sum = 0
    for i = 8, 45 do
        sum = (sum + shown[i] * (i - 7)) % 4096
    end
    put(46, 2, sum)

    pcall(fill, 1, "player", UnitHealth, UnitHealthMax)
    pcall(fill, 2, "player", UnitPower, UnitPowerMax)
    pcall(fill, 3, "target", UnitHealth, UnitHealthMax)
    for i = 1, 4 do
        pcall(fill, 3 + i, "party" .. i, UnitHealth, UnitHealthMax)
    end
    local ok2, err2 = pcall(numbers)                        -- a row that fails part-way is left with a checksum
    local ok3, err3 = pcall(pages)                          -- that no longer fits (it is sealed last): not read
    if not (ok2 and ok3 and slow_ok) then error((not ok2 and err2) or (not ok3 and err3) or slow_err, 0) end
end

set(1, 63) set(2, 0) set(3, 21) set(4, 42)                  -- white, black, 1/3 grey, 2/3 grey
set(5, 48) set(6, 12) set(7, 3)                             -- red, green, blue

local since = 0
f:SetScript("OnUpdate", function(_, dt)
    since = since + dt
    if since >= 0.05 then
        since = 0
        local ok, err = pcall(update)
        if not ok and not f.told then
            f.told = true
            print("|cffff8800Companion Beacon:|r " .. tostring(err))
        end
    end
end)

-- The companion reads the combat and chat logs, and the game switches both off at every logout: switch
-- them on again on the way in (the same as typing /combatlog and /chatlog). Told once per login.
local function logs_on()
    local on = {}
    if LoggingCombat then
        local ok = pcall(LoggingCombat, true)
        if ok and LoggingCombat() then on[#on + 1] = "combat log" end
    end
    if LoggingChat then
        local ok = pcall(LoggingChat, true)
        if ok and LoggingChat() then on[#on + 1] = "chat log" end
    end
    if not f.logs_told then
        f.logs_told = true
        print("|cffff8800Companion Beacon:|r " .. (#on > 0 and (table.concat(on, " + ") .. " on")
            or "could not switch the logs on - type /combatlog and /chatlog"))
    end
end

-- what happened (text 59): who you spoke to, what you gathered, who whispered. Each is only noted - the
-- companion knows where you stood.
local PLACES = { MERCHANT_SHOW = "vendor", TRAINER_SHOW = "trainer", TAXIMAP_OPENED = "flight", MAIL_SHOW = "mail",
                 BANKFRAME_OPENED = "bank", CONFIRM_BINDER = "inn" }
local GATHER = { ["Herb Gathering"] = true, ["Mining"] = true, ["Skinning"] = true, ["Fishing"] = true }
local casting = {}                                          -- cast id -> what it was cast at (a herb, a vein)
local function happened(event, a1, a2, a3, a4)
    if PLACES[event] then
        local kind = PLACES[event]
        if kind == "vendor" and yes(CanMerchantRepair) then kind = "repair" end
        local who = text(UnitName, "npc")
        if who == "" and kind ~= "mail" then who = text(UnitName, "target") end
        note(kind, who)
    elseif event == "CHAT_MSG_WHISPER" then
        note("whisper", plain(a2))
    elseif event == "READY_CHECK" then
        note("ready", plain(a1))
    elseif event == "PLAYER_LEVEL_UP" then
        note("level", tostring(plain(a1) or ""))
    elseif event == "PLAYER_DEAD" then
        note("dead", "")
    elseif event == "UNIT_SPELLCAST_SENT" then
        if plain(a1) == "player" and type(plain(a3)) == "string" then
            casting = {}
            casting[a3] = plain(a2)
        end
    elseif event == "UNIT_SPELLCAST_SUCCEEDED" then
        if plain(a1) == "player" and type(plain(a3)) == "number" then
            local name = spell_name(a3)
            if GATHER[name] then
                local what = type(plain(a2)) == "string" and casting[a2] or ""
                note("gather", name .. ": " .. (type(what) == "string" and what or ""))
            end
        end
    end
end

f:RegisterEvent("PLAYER_ENTERING_WORLD")
f:RegisterEvent("INSPECT_READY")
f:RegisterEvent("DISPLAY_SIZE_CHANGED")
f:RegisterEvent("UI_SCALE_CHANGED")
for _, ev in ipairs({ "MERCHANT_SHOW", "TRAINER_SHOW", "TAXIMAP_OPENED", "MAIL_SHOW", "BANKFRAME_OPENED", "CONFIRM_BINDER",
                      "CHAT_MSG_WHISPER", "READY_CHECK", "PLAYER_LEVEL_UP", "PLAYER_DEAD",
                      "UNIT_SPELLCAST_SENT", "UNIT_SPELLCAST_SUCCEEDED" }) do
    pcall(f.RegisterEvent, f, ev)                           -- (an event this game does not have is left out)
end
f:SetScript("OnEvent", function(_, event, arg1, arg2, arg3, arg4)
    if event == "INSPECT_READY" then
        pcall(inspected, arg1)
        return
    end
    if event ~= "PLAYER_ENTERING_WORLD" and event ~= "DISPLAY_SIZE_CHANGED" and event ~= "UI_SCALE_CHANGED" then
        pcall(happened, event, arg1, arg2, arg3, arg4)
        return
    end
    rescale()
    if event == "PLAYER_ENTERING_WORLD" then
        if type(CompanionBeaconDB) ~= "table" then CompanionBeaconDB = {} end
        pcall(logs_on)
        pcall(rings)
    end
end)
rescale()

SLASH_COMPANIONBEACON1 = "/cbeacon"
SlashCmdList["COMPANIONBEACON"] = function(msg)
    if type(msg) == "string" and msg:lower():match("^%s*rings%s*$") then
        if type(CompanionBeaconDB) ~= "table" then CompanionBeaconDB = {} end
        CompanionBeaconDB.hide_rings = not CompanionBeaconDB.hide_rings
        local ok, can = pcall(rings)
        if not (ok and can) then
            print("|cffff8800Companion Beacon:|r this game has no switch for the quest-area rings")
        elseif CompanionBeaconDB.hide_rings then
            print("|cffff8800Companion Beacon:|r quest-area rings hidden on the minimap (/cbeacon rings again brings them back)")
        else
            print("|cffff8800Companion Beacon:|r quest-area rings will be back after /reload")
        end
        return
    end
    if f:IsShown() then f:Hide() else f:Show() end
    print("|cffff8800Companion Beacon|r " .. (f:IsShown() and "shown" or "hidden"))
end
