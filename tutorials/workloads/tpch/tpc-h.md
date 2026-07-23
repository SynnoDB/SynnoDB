# Global definitions (used by all queries)
## Randomness
- All “random” choices are uniform
- If a parameter appears multiple times in a query, reuse the same value
- Dates are literal calendar dates (YYYY-MM-DD)
- Random seeds are derived from load-time timestamps and stream IDs (precisely defined as mmddhhmmss timestamps, with seed0 + stream_id for throughput streams)

## Core value domains
### Regions (`R_NAME`)
```
AFRICA
AMERICA
ASIA
EUROPE
MIDDLE EAST
```
### Nations (`N_NAME`)
```
ALGERIA
ARGENTINA
BRAZIL
CANADA
EGYPT
ETHIOPIA
FRANCE
GERMANY
INDIA
INDONESIA
IRAN
IRAQ
JAPAN
JORDAN
KENYA
MOROCCO
MOZAMBIQUE
PERU
CHINA
ROMANIA
SAUDI ARABIA
VIETNAM
RUSSIA
UNITED KINGDOM
UNITED STATES
```

### Customer market segments (`C_MKTSEGMENT`)
```
AUTOMOBILE
BUILDING
FURNITURE
HOUSEHOLD
MACHINERY
```

### Ship modes (`L_SHIPMODE`)
```
AIR
AIR REG
RAIL
SHIP
TRUCK
MAIL
FOB
```

### Ship instructions (`L_SHIPINSTRUCT`)
```
DELIVER IN PERSON
COLLECT COD
NONE
TAKE BACK RETURN
```

### Colors (used in part names)
```
almond
antique
aquamarine
azure
beige
bisque
black
blanched
blue
blush
brown
burlywood
burnished
chartreuse
chiffon
chocolate
coral
cornflower
cornsilk
cream
cyan
dark
deep
dim
dodger
drab
firebrick
floral
forest
frosted
gainsboro
ghost
goldenrod
green
grey
honeydew
hot
indian
ivory
khaki
lace
lavender
lawn
lemon
light
lime
linen
magenta
maroon
medium
metallic
midnight
mint
misty
moccasin
navajo
navy
olive
orange
orchid
pale
papaya
peach
peru
pink
plum
powder
puff
purple
red
rose
rosy
royal
saddle
salmon
sandy
seashell
sienna
sky
slate
smoke
snow
spring
steel
tan
thistle
tomato
turquoise
violet
wheat
white
yellow
```

### Part type syllables (3-syllable strings)

Each TYPE is built from three syllables:

#### Syllable 1
```
STANDARD
SMALL
MEDIUM
LARGE
ECONOMY
PROMO
```

#### Syllable 2
```
ANODIZED
BURNISHED
PLATED
POLISHED
BRUSHED
```

#### Syllable 3
```
TIN
NICKEL
BRASS
STEEL
COPPER
```

#### Example full types:

```
PROMO BRUSHED COPPER
STANDARD PLATED STEEL
```

### Containers (2-syllable)
```
SM CASE
SM BOX
SM PACK
SM PKG
MED BAG
MED BOX
MED PACK
MED PKG
LG CASE
LG BOX
LG PACK
LG PKG
```

### Brands
```
Brand#11 … Brand#55
```

Generated as `Brand#MN` where `M,N ∈ [1..5]`

### Country codes (for Q22, derived from phone prefixes)
```
13 31 23 29 30 18 17
```

## Query-by-query substitution parameters
### Q1 – Pricing Summary Report

- DELTA: integer ∈ [60 … 120]

### Q2 – Minimum Cost Supplier
- SIZE: integer ∈ [1 … 50]
- TYPE: syllable 3 only (TIN | NICKEL | BRASS | STEEL | COPPER)
- REGION: from R_NAME

### Q3 – Shipping Priority
- SEGMENT: from C_MKTSEGMENT
- DATE: random day ∈ [1995-03-01 … 1995-03-31]

### Q4 – Order Priority Checking

- DATE: first day of random month ∈ [1993-01 … 1997-10]

### Q5 – Local Supplier Volume
- REGION: from R_NAME
- DATE: YYYY-01-01, year ∈ [1993 … 1997]

### Q6 – Forecasting Revenue Change

- DATE: YYYY-01-01, year ∈ [1993 … 1997]
- DISCOUNT: decimal ∈ [0.02 … 0.09], step 0.01
- QUANTITY: integer ∈ [24 … 25]

### Q7 – Volume Shipping

- NATION1: from N_NAME
- NATION2: from N_NAME, ≠ NATION1

### Q8 – National Market Share

- NATION: from N_NAME
- REGION: from R_NAME
- TYPE: full 3-syllable type

### Q9 – Product Type Profit Measure

- COLOR: from color list

### Q10 – Returned Item Reporting

- DATE: first day of random month ∈ [1993-02 … 1995-01]

### Q11 – Important Stock Identification

- NATION: from N_NAME
- FRACTION: 0.0001 / SF

### Q12 – Shipping Modes and Order Priority

- SHIPMODE1: from L_SHIPMODE
- SHIPMODE2: from L_SHIPMODE, ≠ SHIPMODE1
- DATE: YYYY-01-01, year ∈ [1993 … 1997]

### Q13 – Customer Distribution

- WORD1 ∈ {special, pending, unusual, express}
- WORD2 ∈ {packages, requests, accounts, deposits}

### Q14 – Promotion Effect

- DATE: first day of random month, year ∈ [1993 … 1997]

### Q15 – Top Supplier
- DATE: first day of random month, year ∈ [1993 … 1997]

### Q16 – Parts/Supplier Relationship

- BRAND: Brand#MN (from brand list)
- TYPE: first two syllables only (syllable1 syllable2)
- SIZE1…SIZE8: 8 distinct integers from [1 … 50]

### Q17 – Small-Quantity-Order Revenue

- BRAND: Brand#MN (from brand list)
- CONTAINER: from container list

### Q18 – Large Volume Customer

- QUANTITY: integer ∈ [312 … 315]

### Q19 – Discounted Revenue
- QUANTITY1: ∈ [1 … 10]
- QUANTITY2: ∈ [10 … 20]
- QUANTITY3: ∈ [20 … 30]
- BRAND1/2/3: independent Brand#MN (from brand list)

### Q20 – Potential Part Promotion
- COLOR: from color list
- DATE: YYYY-01-01, year ∈ [1993 … 1997]
- NATION: from N_NAME

### Q21 – Suppliers Who Kept Orders Waiting

- NATION: from N_NAME

### Q22 – Global Sales Opportunity

- I1...I7: 7 distinct values from: country code list

## Notes for implementers / researchers

This parameterization induces non-uniform selectivity despite uniform sampling.
Queries Q11, Q16, Q19 are the most commonly mis-implemented.
Scale factor (SF) only directly affects Q11, but indirectly impacts result cardinalities everywhere.