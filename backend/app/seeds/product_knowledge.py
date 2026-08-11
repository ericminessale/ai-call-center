"""Seed content for the two stock knowledge-base collections.

Data only — :mod:`app.services.knowledge_seed` owns the inserting, and
:mod:`app.services.kb_index` owns getting it into pgvector.

The subject matter is deliberately the SAME fictional shop the bundled
DemoShop MCP server sells (``demo-mcp/shop_seed.py``): desk and
work-from-home gear. A caller who asks "what's your most popular
product?" gets the live catalog through the shop tools and then asks
"how long does its battery last?" — that follow-up lands here. Two
sources of truth about two different things:

  catalog tool  -> what we sell, price, stock, best seller  (live data)
  document KB   -> specs, comparisons, policies, fixes      (these docs)

So NOTHING here quotes a price or a stock level. If a price changes in
the catalog these documents do not go stale, and the sales specialist's
tool description (``_kb_tool_description`` in ai-agents/main_agent.py)
can keep telling the model that prices come from the catalog, full stop.
The one exception is the free-shipping threshold, which is a shipping
policy the catalog has no concept of.

Cloners: this is example content for a shop that does not exist. Replace
it with your own — Settings → Knowledge Base, or edit this file before
first boot. The seed only runs once (see ``SEED_MARKER_KEY``), so your
edits and deletions are never overwritten.

Content notes for editors:
  - Reindexing chunks documents every 5 sentences and embeds each chunk
    alone, so a chunk that says "it weighs 268 grams" is unfindable.
    Name the product again every few sentences — that is why these read
    slightly repetitively.
  - Facts must agree with demo-mcp/shop_seed.py (the product lineup) and
    demo-mcp/shop_mcp_server.py (return rules, carriers). The return
    policy below mirrors what ``start_return`` actually enforces.
"""

from __future__ import annotations

# Agent slugs that each collection is bound to. Matches the assignments
# seeded by migration e5f6a7b8c9d0 / scripts/init.sql.
SALES_AGENTS = ('sales-ai', 'outbound-sales')
SUPPORT_AGENTS = ('support-ai', 'outbound-support')


SALES_DOCUMENTS = [
    (
        "Wireless Over-Ear Headphones (HDPH-001) — product overview",
        """The Wireless Over-Ear Headphones, SKU HDPH-001, are our flagship
headset and our best-selling product overall. They use 40mm dynamic drivers
with hybrid active noise cancellation, which cuts steady background sound like
air conditioning, train noise, and open-plan office hum. The Wireless Over-Ear
Headphones run about 40 hours of playback with noise cancellation off, and
about 30 hours with it on.

Charging the Wireless Over-Ear Headphones takes roughly two hours over USB-C. A
10-minute fast charge gives about four hours of listening, which is the answer
customers usually want when they say they forgot to charge before a flight.
They also work as wired headphones over the included 3.5mm cable, including
when the battery is flat, so they never become useless mid-flight.

Connectivity on the Wireless Over-Ear Headphones is Bluetooth 5.3 with
multipoint: they hold two devices at once, so a laptop call and a phone call
can both reach the customer without re-pairing. The earcups are memory foam
with a replaceable protein-leather cover, and the whole headset weighs 268
grams, which matters to people who wear headphones for a full working day.

Who to recommend the Wireless Over-Ear Headphones to: anyone taking calls all
day, anyone in a noisy room, and anyone who has told you comfort over long
sessions is the priority. Customers who want something pocketable should look
at the True Wireless Earbuds (HDPH-002) instead.""",
    ),
    (
        "True Wireless Earbuds (HDPH-002) — product overview",
        """The True Wireless Earbuds, SKU HDPH-002, are the compact option in our
audio lineup. Each earbud runs about 8 hours on a charge, and the charging case
holds roughly another 24 hours, so a customer gets about 32 hours total before
anything needs a wall socket. The case charges over USB-C and also supports Qi
wireless charging pads.

The True Wireless Earbuds have a transparency mode that mixes in outside sound,
which is what people want for walking near traffic or listening for a doorbell.
They do NOT have active noise cancellation — that is the Wireless Over-Ear
Headphones (HDPH-001). Be straightforward about this: customers who ask for
noise cancellation and are sold earbuds instead come back as returns.

The True Wireless Earbuds are rated IPX4, meaning splash and sweat resistant.
They are fine for the gym and for running in light rain, and they are not for
swimming or the shower. Controls are touch-based on each stem: tap to
play or pause, double-tap to skip, and touch and hold to switch between
transparency mode and normal listening.

Recommend the True Wireless Earbuds to commuters, to people who want something
that disappears in a pocket, and to anyone who dislikes the pressure of
over-ear cups. If the customer's main complaint is background noise on calls,
recommend the Wireless Over-Ear Headphones instead.""",
    ),
    (
        "Mechanical Keyboard, Tactile (KEYB-MECH) — product overview",
        """The Mechanical Keyboard, SKU KEYB-MECH, is a tenkeyless board — 87 keys,
no number pad — with tactile switches. Tactile means there is a noticeable bump
partway through each keypress but no loud click, so it is a reasonable
keyboard to use on a video call. Actuation force is around 45 grams, which is
light enough for long typing sessions.

The Mechanical Keyboard is hot-swappable: switches pull out and push in by
hand, with no soldering, so a customer who decides they want a different feel
later is not stuck with the board they bought. It ships with PBT keycaps, which
resist the shiny wear that cheaper ABS keycaps develop within a year.

The Mechanical Keyboard is wired only, over a detachable USB-C cable, and it
has full N-key rollover so every simultaneous keypress registers. There is a
white LED backlight with several brightness levels; it is not RGB, which is a
deliberate choice for an office-oriented board. There is no Bluetooth on this
model — if a customer needs wireless, say so plainly rather than implying it
might work.

Recommend the Mechanical Keyboard to heavy typists, developers, and anyone
replacing a mushy laptop-style keyboard. The tenkeyless layout is also the
right answer for someone short on desk space or fighting shoulder strain from
reaching too far for a mouse.""",
    ),
    (
        "Ergonomic Vertical Mouse (MOUS-ERG) — product overview",
        """The Ergonomic Vertical Mouse, SKU MOUS-ERG, holds the hand at a 57-degree
angle, close to the neutral handshake position. That is the point of the
product: it takes the twist out of the forearm that a flat mouse forces, which
is what most customers with wrist discomfort are actually reacting to.

The Ergonomic Vertical Mouse connects two ways — a 2.4GHz USB receiver, or
Bluetooth directly to the computer — and can hold both at once, switching with
a button underneath. Sensor resolution steps through 800, 1600, 2400, and 4000
DPI with the button behind the scroll wheel. It has six buttons in total,
including forward and back thumb buttons.

The Ergonomic Vertical Mouse is rechargeable over USB-C and runs several weeks
per charge in normal office use. It keeps working while plugged in, so a flat
battery costs the customer nothing but a cable. It is shaped for the right hand
only — we do not currently make a left-handed version, and telling a
left-handed customer otherwise wastes their time and ours.

Set expectations on the adjustment period: most people need three or four days
before a vertical mouse stops feeling strange, and precision work feels
slightly slower at first. Customers who know that up front keep the mouse;
customers who don't return it on day two.""",
    ),
    (
        "4K Webcam, Auto-focus (WBCM-4K) — product overview",
        """The 4K Webcam, SKU WBCM-4K, records 4K at 30 frames per second, or 1080p
at 60 frames per second for smoother motion. Auto-focus keeps a face sharp when
someone leans in or holds up a document, which is the main upgrade over the
fixed-focus camera built into most laptops.

The 4K Webcam has a 78-degree field of view — wide enough for one person and
their desk, narrow enough that it does not put the whole room on screen. Two
noise-reducing microphones are built in, so it can replace a headset for casual
calls, though anyone on calls all day should still pair it with the Wireless
Over-Ear Headphones.

The 4K Webcam is plug-and-play over USB-C, with a USB-A adapter in the box for
older machines. It needs no drivers on Windows, macOS, or Linux, and works with
Zoom, Teams, Meet, and anything else that accepts a standard USB camera. There
is a physical privacy shutter that slides over the lens, which reassures the
customers who currently have tape on their laptop.

Mounting: the 4K Webcam clips to a monitor or laptop lid, and the clip has a
standard tripod thread underneath for a desk stand or arm. Recommend it to
anyone presenting to customers, recording video, or working from a room where
the light is poor and the laptop camera has given up.""",
    ),
    (
        "Adjustable Laptop Stand (STND-LAP) — product overview",
        """The Adjustable Laptop Stand, SKU STND-LAP, is folded aluminum with six
locking height positions. Raising a screen to eye level is the single cheapest
fix for the neck and shoulder pain that customers describe as "my desk setup is
killing me," so it is worth suggesting whenever someone mentions working from a
laptop all day.

The Adjustable Laptop Stand fits laptops from 11 to 17 inches and holds up to 8
kilograms, about 17.6 pounds, which covers every mainstream laptop including
the heavy 16-inch workstations. The contact points are silicone padded so the
laptop does not slide or scuff, and the open frame lets air reach the underside,
which keeps fan noise down on calls.

The Adjustable Laptop Stand folds flat to about two centimetres and weighs
under a kilogram, so it travels in a laptop bag. Customers who work two days a
week in an office and three at home often buy a second one rather than carry it
back and forth.

One thing to tell customers up front: once the laptop is at eye level, its
built-in keyboard is at the wrong height to type on. A laptop stand almost
always needs an external keyboard and mouse alongside it — this is the natural
moment to mention the Mechanical Keyboard (KEYB-MECH) and the Ergonomic
Vertical Mouse (MOUS-ERG).""",
    ),
    (
        "7-Port USB Hub (HUB-USB) and USB-C Charging Cable (CABL-USBC)",
        """The 7-Port USB Hub with Power, SKU HUB-USB, adds seven USB-A ports to one
upstream connection. Four are USB 3.2 Gen 1 data ports at 5 Gbps, and three are
smart-charging ports that negotiate up to 2.4 amps each for phones and other
devices. Each port has its own switch, so a customer can cut power to a drive
or a hub-powered light without unplugging anything.

The 7-Port USB Hub ships with a 36-watt power adapter, and it matters that
customers actually use it. A powered hub is the difference between seven
devices working and a laptop randomly dropping the ones at the end of the
chain. If a customer describes devices disconnecting on their current hub, an
unpowered hub is usually the reason.

The USB-C Charging Cable, SKU CABL-USBC, is two metres of braided nylon with an
E-marker chip, rated for 100 watts of USB Power Delivery — enough for any
laptop we sell accessories for. It is a charging cable first: data runs at USB
2.0 speed, 480 Mbps.

That distinction is worth stating plainly. The USB-C Charging Cable is the right
answer for charging a laptop, a phone, or the Wireless Over-Ear Headphones, and
the wrong answer for connecting an external SSD or a 4K display. Customers who
buy it expecting fast file transfers are the ones who return it.""",
    ),
    (
        "Choosing between the over-ear headphones and the earbuds",
        """Customers deciding between the Wireless Over-Ear Headphones (HDPH-001) and
the True Wireless Earbuds (HDPH-002) are usually asking one question without
saying it: which one will I actually wear? Ask where they will use them before
recommending either.

Recommend the Wireless Over-Ear Headphones when the customer takes calls for
hours, works in a noisy room, or has said comfort matters. They are the only
one of the two with active noise cancellation, they run about 40 hours per
charge, and they can fall back to a wired connection when the battery dies. The
tradeoff is bulk — they do not go in a pocket.

Recommend the True Wireless Earbuds when portability wins: commuting, the gym,
walking, or a bag that is already full. They are the lower-cost of the two,
they are splash resistant at IPX4, and they give about 32 hours total counting
the charging case. The tradeoff is that they have transparency mode but no
noise cancellation, and small ears sometimes struggle with fit.

If a customer genuinely does both — desk calls all day and a noisy commute —
say so honestly: the two products solve different problems and plenty of people
own both. Do not oversell the earbuds to someone who opened the call by
complaining about background noise on their meetings.""",
    ),
    (
        "Work-from-home setups — what to recommend together",
        """When a customer asks what they should buy for working from home, what
they need for a home office, or how to set up a desk for remote work, recommend
by problem rather than by product. Customers rarely call asking for a bundle;
they call describing something that is bothering them about their desk. Listen
for that problem and recommend the two or three products that solve it, rather
than reading out the catalogue.

Neck and shoulder pain from a laptop: the Adjustable Laptop Stand (STND-LAP)
raises the screen, and because that puts the built-in keyboard out of reach, it
needs the Mechanical Keyboard (KEYB-MECH) and the Ergonomic Vertical Mouse
(MOUS-ERG) alongside it. This is the most common three-product combination we
sell and the one with the fewest returns.

"I sound and look terrible on video calls": the 4K Webcam (WBCM-4K) fixes the
picture and the Wireless Over-Ear Headphones (HDPH-001) fix the audio in both
directions — the customer hears better and stops broadcasting their room.

"My desk is a mess of cables" or "my laptop only has two ports": the 7-Port USB
Hub with Power (HUB-USB) consolidates everything onto one connection, and the
USB-C Charging Cable (CABL-USBC) is worth adding when their laptop charger cable
is too short to reach a raised stand.

Someone setting up a home office from scratch generally ends up with a stand,
keyboard, mouse, and a headset. Suggest the pieces in that order — the stand
changes their posture, and the rest follows from it.""",
    ),
    (
        "Shipping, delivery, and changing an order",
        """Standard shipping takes 3 to 5 business days and is free on orders over
$50. Express shipping is 1 to 2 business days for a flat fee, and orders placed
before 2pm local time ship the same business day. We ship with UPS, FedEx, and
USPS, and the carrier is chosen by destination, so a customer cannot request a
specific one.

Every order gets a tracking number by email once it leaves the warehouse. On a
call, look the order up directly rather than asking the customer to find the
email — the order tools return current status and the full tracking timeline.
An order that has not shipped yet has no tracking number, and saying so is
better than promising one will appear shortly.

Changing an order is possible only before it ships. That includes the delivery
address, which is the change customers ask for most and the one that causes the
most trouble once a parcel is moving. If the order has already shipped, the
customer's options are to refuse delivery or to return it once it arrives.

An order that has not shipped can be cancelled outright for a full refund. An
order that has shipped cannot be cancelled — it becomes a return, which is a
different process with its own rules. See the support knowledge base for how
returns and refunds work.""",
    ),
    (
        "Warranty and the 30-day satisfaction guarantee",
        """Every product we sell carries a 30-day satisfaction guarantee. If a
customer does not get on with something within 30 days of delivery, it comes
back for a full refund — no fault required and no explanation needed. This is
worth mentioning to hesitant customers, particularly for the Ergonomic Vertical
Mouse (MOUS-ERG), which takes a few days to get used to.

Beyond that, the hardware warranty is 2 years on the Wireless Over-Ear
Headphones, the True Wireless Earbuds, the Mechanical Keyboard, the Ergonomic
Vertical Mouse, the 4K Webcam, the Adjustable Laptop Stand, and the 7-Port USB
Hub. The USB-C Charging Cable carries a 1-year warranty. The warranty covers
manufacturing defects and hardware failure in normal use.

The warranty does not cover accidental damage, liquid damage beyond a product's
rating, or wear items. Ear cushions on the Wireless Over-Ear Headphones and
keycaps on the Mechanical Keyboard are wear items and are sold as replacement
parts instead.

Warranty service does not require the original packaging or the receipt, as
long as the order is in the customer's account. A defective product inside
warranty is replaced, not repaired. Do not promise a specific replacement
timeline on the call — say a replacement is arranged and let the confirmation
email carry the dates.""",
    ),
]


SUPPORT_DOCUMENTS = [
    (
        "Headphones or earbuds will not pair, or keep disconnecting",
        """Pairing problems with the Wireless Over-Ear Headphones (HDPH-001) and the
True Wireless Earbuds (HDPH-002) are almost always one of three things: the
headset is connected to a different device, the phone is holding a stale
pairing record, or the headset never entered pairing mode.

Start by asking what else the headset has been paired with recently. The
Wireless Over-Ear Headphones hold two devices at once through multipoint, so a
laptop in another room can quietly claim them. Turning Bluetooth off on that
other device is the fastest test, and it resolves this more often than anything
else.

To put the Wireless Over-Ear Headphones into pairing mode, hold the power
button for about 5 seconds until the light alternates between two colours. For
the True Wireless Earbuds, put both earbuds in the case, leave the lid open,
and hold the case button for about 5 seconds until the case light flashes.

If the headset is in pairing mode and the phone still will not connect, have
the customer remove or "forget" the old entry in their Bluetooth settings
before trying again. A stale record is invisible to the customer and blocks the
new connection silently.

For audio that connects but keeps cutting out, the usual cause is distance or
interference rather than a fault. Ask the customer to move within a couple of
metres of the device with nothing solid in between. Dropouts only in a specific
room, or only near a microwave or a busy router, point at interference. If it
drops out everywhere and on more than one device, treat it as a hardware
fault and go to the warranty process.""",
    ),
    (
        "Headphone and earbud battery or charging problems",
        """When a customer says the Wireless Over-Ear Headphones (HDPH-001) are not
holding charge, establish what "not holding charge" means before troubleshooting.
Expected life is about 40 hours with noise cancellation off and about 30 hours
with it on. Someone getting 25 hours with noise cancellation on all day is
within normal range; someone getting 3 hours is not.

Check the cable and the port first. The Wireless Over-Ear Headphones charge
over USB-C, and a cable that only carries power to a phone can still fail here.
Have the customer try a different cable and a different power source — a wall
adapter rather than a laptop port, which may be asleep. A full charge takes
about two hours, and a 10-minute charge should give roughly four hours of use.

For the True Wireless Earbuds (HDPH-002), ask whether one earbud dies before
the other. That is normally a case-contact problem, not a battery problem: the
charging pins in the case pick up earwax and pocket lint, and the affected
earbud never actually charges. Cleaning both the case pins and the metal
contacts on the earbuds with a dry cotton bud fixes most of these.

If the True Wireless Earbuds case itself is not charging, check that the case
lid closes fully with the earbuds seated — a misaligned earbud holds the lid
open a millimetre and stops the charge cycle. Genuine capacity loss well inside
the 2-year warranty is a warranty replacement, not something to keep
troubleshooting.""",
    ),
    (
        "Mechanical keyboard: keys repeating, dead, or typing wrong characters",
        """A key on the Mechanical Keyboard (KEYB-MECH) that repeats characters or
registers twice per press is usually switch chatter, and this board is
hot-swappable, which makes it a quick fix. Have the customer pull the keycap,
pull the switch straight up with the puller in the box, and reseat it. If it
still chatters, moving that switch to a rarely used key confirms whether the
fault follows the switch — it usually does, and a replacement switch is a
warranty part.

A completely dead key on the Mechanical Keyboard is the same procedure, but
check the switch pins first. A bent pin under a reseated switch is common after
a customer has already tried this once, and a bent pin never makes contact no
matter how hard the key is pressed.

If the Mechanical Keyboard types the wrong characters — quotes and @ swapped,
or # appearing as £ — nothing is wrong with the keyboard. That is the operating
system's keyboard layout set to the wrong region. Walk the customer to their
system keyboard settings and switch between US and UK layouts.

If the whole Mechanical Keyboard is dead, remember it is wired only, over a
detachable USB-C cable. The cable comes loose at the keyboard end more often
than customers expect. Try reseating both ends, then a different USB port
directly on the computer rather than through a hub.

For a keyboard that works but is intermittent through a hub, plug it straight
into the computer. An unpowered hub sharing bandwidth with a drive or webcam
causes exactly this, and the 7-Port USB Hub (HUB-USB) must be running on its
own power adapter.""",
    ),
    (
        "Vertical mouse: cursor stutter, wrong buttons, or no connection",
        """The Ergonomic Vertical Mouse (MOUS-ERG) connects either through its 2.4GHz
USB receiver or over Bluetooth, and it holds both at once with a switch
underneath. The first question for any connection problem is which mode the
customer is on, because the fixes are different. If the mouse works on one mode
and not the other, that is a setup problem, not a fault.

For stuttering or jumping on the Ergonomic Vertical Mouse, look at the surface
first. Optical sensors struggle on glass, on high-gloss desks, and on dark
uniform surfaces. A sheet of paper under the mouse is a five-second test that
settles it. If the customer is on the 2.4GHz receiver and it is plugged into
the back of a desktop or into a crowded hub, moving the receiver to a front
port or a short extension usually clears it.

If the Ergonomic Vertical Mouse has stopped responding entirely, charge it over
USB-C for a few minutes and try again — it stays usable while plugged in, so a
mouse that works on the cable and not off it is simply flat, or has a failing
battery if it discharges within days.

For buttons doing the wrong thing, check whether the customer has installed
third-party button-mapping software, and remember the DPI button behind the
scroll wheel steps through 800, 1600, 2400, and 4000 DPI. A customer who says
the mouse "suddenly got too fast" has almost always brushed that button.

Finally: the Ergonomic Vertical Mouse is right-handed only. A left-handed
customer struggling with it does not have a defective unit, and the honest
answer is the 30-day satisfaction guarantee.""",
    ),
    (
        "4K webcam: not detected, blurry, dark, or wrong camera in the call",
        """When the 4K Webcam (WBCM-4K) does not appear at all, the most common cause
is another application holding the camera. Only one program can use a camera at
a time on most systems, so a Zoom window left open in the background blocks
Teams from seeing it. Closing every other video app and rejoining the call
resolves a large share of these.

The 4K Webcam is plug-and-play and needs no drivers, so if it is still missing,
move it to a USB port directly on the computer rather than through a hub or a
monitor's port. If the customer is using the bundled USB-A adapter, try without
it on a machine that has USB-C. A camera that shows up on a second computer is
working, which points the investigation at the first computer.

For a blurry picture from the 4K Webcam, check the privacy shutter is fully
open — a partly closed shutter reads as soft focus rather than a black frame —
and that the lens is clean. Auto-focus needs some contrast to lock onto, so a
customer sitting against a blank wall in low light may see it hunt.

For a dark image, the fix is light in front of the customer, not behind them. A
window behind the customer makes them a silhouette no camera can rescue. Ask
them to turn to face the window, or put a lamp behind the screen pointing at
themselves.

If the video call shows the laptop's built-in camera instead, that is a setting
inside the meeting application, not a fault. Walk them to the video settings in
that specific app and select the 4K Webcam by name — many applications remember
a per-app choice and will not follow the system default.""",
    ),
    (
        "USB hub and cable: devices dropping out, slow charging, or no display",
        """Devices disconnecting from the 7-Port USB Hub (HUB-USB) is nearly always a
power problem. Confirm the customer has the 36-watt adapter plugged in — the
hub will partly work without it, which is exactly what makes this confusing,
and drives and webcams are the first things to drop when power runs short.

Each port on the 7-Port USB Hub has its own switch, and customers do knock them
off. Before deeper troubleshooting, ask the customer to check the switch for
the port that stopped working. Also worth knowing: the four USB 3.2 Gen 1 ports
carry data at 5 Gbps, while the other three are smart-charging ports — a drive
plugged into a charging port may charge and never appear.

For the USB-C Charging Cable (CABL-USBC), set expectations correctly. It is a
100-watt Power Delivery charging cable with USB 2.0 data speed, 480 Mbps. A
customer reporting slow file transfers from an external SSD does not have a
faulty cable; they have the wrong cable for that job, and no amount of
troubleshooting will change it.

The same applies to video: the USB-C Charging Cable does not carry DisplayPort,
so it cannot drive an external monitor. If a customer plugged it into a
monitor and got nothing, that is expected behaviour.

For genuinely slow charging over the USB-C Charging Cable, the limit is usually
the power adapter rather than the cable. Ask what wattage the adapter is. The
cable supports up to 100 watts, but a 20-watt phone charger will only ever
deliver 20 watts to a laptop.""",
    ),
    (
        "Laptop stand: wobble, height adjustment, and weight limits",
        """A wobbling Adjustable Laptop Stand (STND-LAP) is usually a height position
that has not clicked fully into its lock, rather than a bent frame. Ask the
customer to lift the laptop off, work the stand through its full range, and
seat it firmly in one of the six positions until it stops with a click.

Check the surface as well. The Adjustable Laptop Stand has silicone feet that
grip a hard desk; on a thick desk mat or an uneven surface it can rock slightly
even when correctly locked. Moving it onto the bare desk isolates that in
seconds.

The Adjustable Laptop Stand holds up to 8 kilograms, about 17.6 pounds, and
fits laptops from 11 to 17 inches. Customers occasionally use it for a small
monitor, which is over the design limit and unstable — that is not a warranty
case, and it is worth flagging kindly before something falls.

If the height adjustment has become stiff or will not hold, look for grit in
the hinge from travelling in a bag. A clean with a dry cloth and a small amount
of movement usually restores it. A hinge that has lost tension and will not
hold the laptop at any position within the 2-year warranty is a replacement.

One thing customers report as a fault that is not one: the laptop's own
keyboard becomes awkward to type on once the stand raises the screen. That is
the stand working as designed, and the answer is an external keyboard and
mouse, not a return.""",
    ),
    (
        "Returns, RMAs, and refunds — how the process works",
        """A customer can return anything within 30 days of delivery under our
satisfaction guarantee, for a full refund, with no fault required. On the call
you can open the return yourself against their order — you do not need to send
them to a web form, and doing it live is faster for everyone.

Opening a return produces an RMA number. Read it back to the customer and tell
them the return label goes to the email on the order. They then have 14 days to
drop the package at any carrier location. The refund is issued once the package
reaches us, and it lands on the original payment method within 3 to 5 business
days after that.

Some orders cannot be returned, and it is better to explain why than to try and
fail in front of the customer. An order that has not shipped yet cannot be
returned — it should be cancelled instead, which is faster and refunds in full.
An order already marked returned or cancelled cannot be returned again; if the
customer believes otherwise, look the order up rather than arguing.

If a customer wants to return only part of a multi-item order, or is past the
30-day window and pushing for an exception, that is a human decision, not an AI
one. Escalate rather than promising something we may not honour.

Returns for a defective product inside warranty follow this same process but are
warranty replacements rather than refunds — see the warranty guidance in the
sales knowledge base for what is covered and what counts as a wear item.""",
    ),
    (
        "Firmware updates, factory reset, and when to stop troubleshooting",
        """A factory reset clears pairing records and settings and is the last step
before treating something as a hardware fault. For the Wireless Over-Ear
Headphones (HDPH-001), hold the power and volume-down buttons together for
about 10 seconds until the light flashes and the headset powers off. For the
True Wireless Earbuds (HDPH-002), put both earbuds in the case with the lid
open and hold the case button for about 15 seconds until the light flashes
twice.

After a factory reset, every previously paired device must be re-paired, and
the customer should remove the old entry from each device's Bluetooth list
first. Warn them before the reset, not after — a customer who loses their
phone, laptop, and tablet pairings without warning treats it as a new problem.

Firmware for the Wireless Over-Ear Headphones, the True Wireless Earbuds, and
the Ergonomic Vertical Mouse installs through our desktop companion
application, which the customer downloads from our website. Firmware updates
are not automatic. Ask the customer to keep the device connected and charged
above 50 percent while an update runs, and not to close the laptop lid.

The Mechanical Keyboard (KEYB-MECH), the 4K Webcam (WBCM-4K), the 7-Port USB
Hub (HUB-USB), the USB-C Charging Cable (CABL-USBC), and the Adjustable Laptop
Stand (STND-LAP) have no firmware and no factory reset. If a customer has been
told to update firmware on any of these, that advice was wrong.

Know when to stop. If a factory reset has not fixed it, if the same fault
follows the product to a second computer, or if you have tried two or three
approaches without progress, it is a hardware fault. Move to a warranty
replacement or hand the call to a human specialist rather than repeating steps
the customer has already been through.""",
    ),
]


# Collection seed definitions. ``name``/``display_name``/``description``
# match what migration e5f6a7b8c9d0 and scripts/init.sql already create,
# so an existing deployment keeps its rows and only gains documents.
SEED_COLLECTIONS = (
    {
        'name': 'sales_knowledge',
        'display_name': 'Sales Knowledge Base',
        'description': 'Product info, pricing, sales scripts',
        'agents': SALES_AGENTS,
        'documents': SALES_DOCUMENTS,
    },
    {
        'name': 'support_knowledge',
        'display_name': 'Support Knowledge Base',
        'description': 'Troubleshooting guides, FAQs, diagnostics',
        'agents': SUPPORT_AGENTS,
        'documents': SUPPORT_DOCUMENTS,
    },
)
