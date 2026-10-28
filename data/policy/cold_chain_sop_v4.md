# Cold Chain Operations Standard Operating Procedure (SOP) v4

Document ID: CC-SOP-004 · Revision 4.0 · Owner: Network Operations Control (NOC)
Applies to: all temperature-controlled road shipments dispatched from company hubs and partner carriers.

## 1. Purpose and Scope

1.1 This SOP defines how dispatchers, drivers, and hub staff monitor, protect, and recover temperature-sensitive freight from origin loading through final delivery.

1.2 It covers pharmaceutical products (vaccines, biologics, insulin, blood plasma, mRNA vaccines), food products (fresh and frozen seafood, dairy, fresh produce, ice cream), and floral cargo.

1.3 Where a customer quality agreement is stricter than this SOP, the customer agreement prevails. Where this SOP is stricter, this SOP prevails.

## 2. Definitions

2.1 **Target band**: the inclusive temperature range (`target_min_temp_c` to `target_max_temp_c`) the reefer must hold for the cargo class.

2.2 **Temperature excursion**: any reefer reading strictly below the target minimum or strictly above the target maximum.

2.3 **Excursion magnitude**: the absolute distance in °C between the reading and the nearest band edge.

2.4 **Excursion duration**: minutes elapsed since the first out-of-band reading, measured from telemetry pings.

2.5 **Active shipment**: any shipment whose status is not DELIVERED (statuses: LOADING, IN_TRANSIT, AT_HUB, DELAYED_WEATHER).

2.6 **Quality hold**: a documented quarantine of product pending Quality Assurance (QA) disposition. Product on quality hold must not be delivered to the consignee.

## 3. Temperature Bands by Cargo Class

| Cargo class      | Target band (°C) | Pharma-grade | Max tolerated excursion before QA hold |
|------------------|------------------|--------------|----------------------------------------|
| Vaccines         | 2 to 8           | Yes          | 30 minutes or 1.0 °C magnitude          |
| Biologics        | 2 to 8           | Yes          | 30 minutes or 1.0 °C magnitude          |
| Insulin          | 2 to 8           | Yes          | 60 minutes or 2.0 °C magnitude          |
| mRNA Vaccines    | -25 to -15       | Yes          | 15 minutes or any warm-side excursion   |
| Blood Plasma     | -30 to -18       | Yes          | 15 minutes or any warm-side excursion   |
| Fresh Seafood    | -1 to 2          | No           | 60 minutes or 2.0 °C magnitude          |
| Frozen Seafood   | -25 to -18       | No           | 120 minutes or 3.0 °C magnitude         |
| Dairy            | 1 to 4           | No           | 90 minutes or 2.0 °C magnitude          |
| Fresh Produce    | 1 to 4           | No           | 120 minutes or 3.0 °C magnitude         |
| Ice Cream        | -28 to -20       | No           | 30 minutes or any reading above -15 °C  |
| Cut Flowers      | 1 to 3           | No           | 120 minutes or 3.0 °C magnitude         |

3.1 "Whichever comes first" applies to the duration and magnitude limits in the table.

3.2 Pharma-grade cargo is always handled as Severity 2 or higher on any excursion (see Section 4).

## 4. Temperature Excursion Response

4.1 **Severity 1 (Minor)**: non-pharma cargo, magnitude below 1.0 °C, duration under 30 minutes. Dispatcher actions: confirm the reading with the driver, verify the reefer setpoint and door status, and monitor every 10 minutes until back in band.

4.2 **Severity 2 (Major)**: any pharma-grade excursion, or non-pharma magnitude of 1.0 °C or more. Dispatcher actions within 15 minutes:
   a) Instruct the driver to stop at the nearest safe location and verify the reefer unit, setpoint, and door seals.
   b) If the door is OPEN, the driver must close and seal it immediately and photograph the seal.
   c) Notify the NOC Shift Lead and the customer Quality contact.
   d) Open an excursion record in the Quality Management System (QMS) with the shipment ID, readings, and timestamps.

4.3 **Severity 3 (Critical)**: the excursion exceeds the "max tolerated" limit in Section 3, or any warm-side excursion on mRNA Vaccines or Blood Plasma. Dispatcher actions within 10 minutes:
   a) Place the shipment on quality hold; delivery to the consignee is prohibited until QA releases it.
   b) Divert to the nearest qualified cold-chain hub (Section 7) if the reefer cannot recover the band within 30 minutes.
   c) Escalate to the Regional Operations Manager and QA Duty Officer (Section 8).
   d) Preserve all telemetry; it is evidence for the product disposition decision (Section 10).

4.4 An excursion is closed only after three consecutive in-band readings at least 5 minutes apart.

## 5. Reefer Equipment, Doors, and Power

5.1 **Door discipline**: reefer doors may be open only during loading or unloading at a dock with an active cold curtain. A door reported OPEN while the shipment is IN_TRANSIT, AT_HUB without dock assignment, or DELAYED_WEATHER is a Severity 2 event even if temperature is still in band.

5.2 **Battery thresholds** (reefer and telematics unit):
   a) Below 25 %: warning. The dispatcher confirms with the driver that the unit is on engine or shore power and schedules a charge stop within 2 hours.
   b) Below 15 %: critical. Treat as imminent loss of temperature control; arrange shore power or a hub stop within 45 minutes.
   c) A battery below 25 % combined with an active excursion escalates the excursion by one severity level.

5.3 **Telemetry gaps**: no ping for 20 minutes requires a driver call. No ping for 45 minutes on pharma-grade cargo requires a Severity 2 record.

5.4 Setpoints must never be changed by the driver without dispatcher approval logged in the QMS.

## 6. Weather Risk Management

6.1 Dispatch checks live weather at the truck's current position for every Severity 2 or 3 excursion and for every shipment in DELAYED_WEATHER status.

6.2 **Wind**:
   a) Wind gusts of 50 km/h or more: caution. Reduce speed, avoid exposed passes and bridges where alternatives exist, and increase monitoring to every 10 minutes.
   b) Wind gusts of 65 km/h or more: mandatory weather hold for high-profile reefer trailers. The driver parks at the nearest safe, powered location and the status is set to DELAYED_WEATHER.

6.3 **Precipitation**: 7.5 mm/h or more (heavy rain) or any freezing rain requires a weather hold on mountain routes and a 25 % speed reduction elsewhere.

6.4 **Ambient cold**: ambient temperature at or below -10 °C requires heater-mode verification for Dairy, Fresh Produce, Fresh Seafood, Cut Flowers, and all 2 to 8 °C pharma cargo, because these classes can freeze-damage.

6.5 **Ambient heat**: ambient temperature at or above 35 °C requires pre-cooled dock handoffs only and a door-open limit of 5 minutes.

6.6 A weather hold that pushes the ETA beyond the customer delivery window must be communicated to the customer within 30 minutes of the hold.

6.7 A truck in DELAYED_WEATHER status that is also in excursion is handled at the higher of the two response levels, and diversion (Section 7) takes priority over waiting out the weather when the reefer cannot hold the band.

## 7. Rerouting and Hub Diversion

7.1 Qualified cold-chain diversion hubs: Memphis TN, Louisville KY, Chicago IL, Atlanta GA, Dallas TX, Denver CO, Indianapolis IN, Kansas City MO, Salt Lake City UT, Newark NJ.

7.2 Divert to the nearest qualified hub by road distance when any of the following apply:
   a) A Severity 3 excursion cannot be recovered within 30 minutes.
   b) Battery is below 15 % and no shore power is reachable within 45 minutes.
   c) A mandatory weather hold (6.2 b) is expected to last longer than the cargo's max tolerated excursion window.

7.3 On arrival, the hub transfers product to a pre-conditioned chamber within 20 minutes and records chamber temperature at transfer.

## 8. Communication and Escalation Matrix

| Trigger                                   | Notify within | Roles                                                    |
|-------------------------------------------|---------------|----------------------------------------------------------|
| Severity 1                                | 60 minutes    | NOC Dispatcher (log only)                                |
| Severity 2                                | 15 minutes    | NOC Shift Lead, customer Quality contact                 |
| Severity 3                                | 10 minutes    | Regional Operations Manager, QA Duty Officer, customer   |
| Weather hold beyond delivery window       | 30 minutes    | Customer Service, customer logistics contact             |
| Battery critical (below 15 %)             | 15 minutes    | NOC Shift Lead, carrier maintenance desk                 |
| Suspected tampering or seal breach        | Immediately   | Security Operations, QA Duty Officer                     |

8.1 All notifications reference the shipment ID, truck ID, current reading, target band, and the SOP section invoked.

## 9. Documentation and Audit

9.1 Every dispatcher decision on an excursion, weather hold, or diversion is recorded in the QMS with timestamp, actor, and rationale.

9.2 Automated assistants used by dispatch must log each data query and tool call to the immutable audit trail. Audit records are append-only and may not be edited or deleted.

9.3 Commercial terms, rates, and margins are confidential and must not appear in operational communications, excursion records, or assistant outputs.

9.4 Records are retained for 7 years for pharma-grade cargo and 2 years for all other cargo.

## 10. Product Disposition

10.1 Only QA may release product from quality hold. Dispatch must never instruct delivery of product on hold.

10.2 Disposition options: release, release with customer concession, return to origin, or destroy. Destruction requires a witnessed certificate.

10.3 For pharma-grade cargo, QA reviews the full temperature history against the manufacturer's stability data before release.
