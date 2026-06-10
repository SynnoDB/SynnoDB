ceb_templates = {
    "Q1a": """SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (mi1.info IN INFO1)
AND (mi2.info IN INFO2)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)""",
    "Q2a": """SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n,
movie_keyword as mk,
keyword as k
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND t.id = mk.movie_id
AND k.id = mk.keyword_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (mi1.info IN INFO1)
AND (mi2.info IN INFO2)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)""",
    "Q2b": """SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n,
movie_keyword as mk,
keyword as k
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND t.id = mk.movie_id
AND k.id = mk.keyword_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (mi1.info IN INFO1)
AND (mi2.info IN INFO2)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (k.keyword IN KEYWORD)""",
    "Q2c": """SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
movie_info as mi2,
info_type as it2,
cast_info as ci,
role_type as rt,
name as n
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND t.id = mi2.movie_id
AND mi1.movie_id = mi2.movie_id
AND mi1.info_type_id = it1.id
AND mi2.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (mi1.info IN INFO1)
AND (mi2.info IN INFO2)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (t.title IN TITLE)""",
    "Q3a": """SELECT COUNT(*) FROM title as t,
movie_keyword as mk, keyword as k,
movie_companies as mc, company_name as cn,
company_type as ct, kind_type as kt,
cast_info as ci, name as n, role_type as rt
WHERE t.id = mk.movie_id
AND t.id = mc.movie_id
AND t.id = ci.movie_id
AND ci.movie_id = mc.movie_id
AND ci.movie_id = mk.movie_id
AND mk.movie_id = mc.movie_id
AND k.id = mk.keyword_id
AND cn.id = mc.company_id
AND ct.id = mc.company_type_id
AND kt.id = t.kind_id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (k.keyword IN KEYWORD)
AND (cn.country_code IN COUNTRY)
AND (ct.kind IN KIND1)
AND (kt.kind IN KIND2)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)""",
    "Q3b": """SELECT t.title, n.name, cn.name, COUNT(*)
FROM title as t,
movie_keyword as mk,
keyword as k,
movie_companies as mc,
company_name as cn,
company_type as ct,
kind_type as kt,
cast_info as ci,
name as n,
role_type as rt
WHERE t.id = mk.movie_id
AND t.id = mc.movie_id
AND t.id = ci.movie_id
AND ci.movie_id = mc.movie_id
AND ci.movie_id = mk.movie_id
AND mk.movie_id = mc.movie_id
AND k.id = mk.keyword_id
AND cn.id = mc.company_id
AND ct.id = mc.company_type_id
AND kt.id = t.kind_id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (t.title ILIKE TITLE)
AND (n.name_pcode_nf ILIKE NAME_PCODE_NF)
AND (cn.name ILIKE NAME)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
GROUP BY t.title, n.name, cn.name
ORDER BY COUNT(*) DESC""",
    "Q4a": r"""SELECT COUNT(*)
FROM
name as n,
aka_name as an,
info_type as it1,
person_info as pi1,
cast_info as ci,
role_type as rt
WHERE
n.id = ci.person_id
AND ci.person_id = pi1.person_id
AND it1.id = pi1.info_type_id
AND n.id = pi1.person_id
AND n.id = an.person_id
AND ci.person_id = an.person_id
AND an.person_id = pi1.person_id
AND rt.id = ci.role_id
AND (n.gender IN GENDER)
AND (n.name_pcode_nf IN NAME_PCODE_NF)
AND (ci.note IN NOTE)
AND (rt.role IN ROLE)
AND (it1.id IN ID)""",
    "Q5a": r"""SELECT COUNT(*)
FROM title as t,
movie_info as mi1,
kind_type as kt,
info_type as it1,
info_type as it3,
info_type as it4,
movie_info_idx as mii1,
movie_info_idx as mii2,
movie_keyword as mk,
keyword as k
WHERE
t.id = mi1.movie_id
AND t.id = mii1.movie_id
AND t.id = mii2.movie_id
AND t.id = mk.movie_id
AND mii2.movie_id = mii1.movie_id
AND mi1.movie_id = mii1.movie_id
AND mk.movie_id = mi1.movie_id
AND mk.keyword_id = k.id
AND mi1.info_type_id = it1.id
AND mii1.info_type_id = it3.id
AND mii2.info_type_id = it4.id
AND t.kind_id = kt.id
AND (kt.kind IN KIND)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (mi1.info IN INFO1)
AND (it1.id IN ID1)
AND it3.id = ID2
AND it4.id = ID3
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii2.info::float <= INFO2)
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO3 <= mii2.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO4 <= mii1.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii1.info::float <= INFO5)""",
    "Q6a": r"""SELECT COUNT(*)
FROM title as t,
movie_info as mi1,
kind_type as kt,
info_type as it1,
info_type as it3,
info_type as it4,
movie_info_idx as mii1,
movie_info_idx as mii2,
aka_name as an,
name as n,
info_type as it5,
person_info as pi1,
cast_info as ci,
role_type as rt
WHERE
t.id = mi1.movie_id
AND t.id = ci.movie_id
AND t.id = mii1.movie_id
AND t.id = mii2.movie_id
AND mii2.movie_id = mii1.movie_id
AND mi1.movie_id = mii1.movie_id
AND mi1.info_type_id = it1.id
AND mii1.info_type_id = it3.id
AND mii2.info_type_id = it4.id
AND t.kind_id = kt.id
AND (kt.kind IN KIND)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (mi1.info IN INFO1)
AND (it1.id IN ID1)
AND it3.id = ID2
AND it4.id = ID3
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii2.info::float <= INFO2)
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO3 <= mii2.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO4 <= mii1.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii1.info::float <= INFO5)
AND n.id = ci.person_id
AND ci.person_id = pi1.person_id
AND it5.id = pi1.info_type_id
AND n.id = pi1.person_id
AND n.id = an.person_id
AND ci.person_id = an.person_id
AND an.person_id = pi1.person_id
AND rt.id = ci.role_id
AND (n.gender IN GENDER)
AND (n.name_pcode_nf IN NAME_PCODE_NF)
AND (ci.note IN NOTE)
AND (rt.role IN ROLE)
AND (it5.id IN ID4)""",
    "Q7a": r"""SELECT COUNT(*)
FROM title as t,
movie_info as mi1,
kind_type as kt,
info_type as it1,
info_type as it3,
info_type as it4,
movie_info_idx as mii1,
movie_info_idx as mii2,
movie_keyword as mk,
keyword as k,
aka_name as an,
name as n,
info_type as it5,
person_info as pi1,
cast_info as ci,
role_type as rt
WHERE
t.id = mi1.movie_id
AND t.id = ci.movie_id
AND t.id = mii1.movie_id
AND t.id = mii2.movie_id
AND t.id = mk.movie_id
AND mk.keyword_id = k.id
AND mi1.info_type_id = it1.id
AND mii1.info_type_id = it3.id
AND mii2.info_type_id = it4.id
AND t.kind_id = kt.id
AND (kt.kind IN KIND)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (mi1.info IN INFO1)
AND (it1.id IN ID1)
AND it3.id = ID2
AND it4.id = ID3
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii2.info::float <= INFO2)
AND (mii2.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO3 <= mii2.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND INFO4 <= mii1.info::float)
AND (mii1.info ~ '^(?:[1-9]\d*|0)?(?:\.\d+)?$' AND mii1.info::float <= INFO5)
AND n.id = ci.person_id
AND ci.person_id = pi1.person_id
AND it5.id = pi1.info_type_id
AND n.id = pi1.person_id
AND n.id = an.person_id
AND rt.id = ci.role_id
AND (n.gender IN GENDER)
AND (n.name_pcode_nf IN NAME_PCODE_NF)
AND (ci.note IN NOTE)
AND (rt.role IN ROLE)
AND (it5.id IN ID4)""",
    "Q8a": r"""SELECT COUNT(*) FROM title as t,
kind_type as kt,
info_type as it1,
movie_info as mi1,
cast_info as ci,
role_type as rt,
name as n,
movie_keyword as mk,
keyword as k,
movie_companies as mc,
company_type as ct,
company_name as cn
WHERE
t.id = ci.movie_id
AND t.id = mc.movie_id
AND t.id = mi1.movie_id
AND t.id = mk.movie_id
AND mc.company_type_id = ct.id
AND mc.company_id = cn.id
AND k.id = mk.keyword_id
AND mi1.info_type_id = it1.id
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND (it1.id IN ID)
AND (mi1.info IN INFO)
AND (kt.kind IN KIND1)
AND (rt.role IN ROLE)
AND (n.gender IN GENDER)
AND (n.name_pcode_cf IN NAME_PCODE_CF)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (cn.name IN NAME)
AND (ct.kind IN KIND2)""",
    "Q9a": r"""SELECT mi1.info, pi.info, COUNT(*)
FROM title as t,
kind_type as kt,
movie_info as mi1,
info_type as it1,
cast_info as ci,
role_type as rt,
name as n,
info_type as it2,
person_info as pi
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND mi1.info_type_id = it1.id
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.movie_id = mi1.movie_id
AND ci.role_id = rt.id
AND n.id = pi.person_id
AND pi.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND (mi1.info ILIKE INFO1)
AND (pi.info ILIKE INFO2)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
GROUP BY mi1.info, pi.info""",
    "Q9b": """SELECT mi1.info, n.name, COUNT(*)
FROM title as t,
kind_type as kt,
movie_info as mi1,
info_type as it1,
cast_info as ci,
role_type as rt,
name as n,
info_type as it2,
person_info as pi
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND mi1.info_type_id = it1.id
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.movie_id = mi1.movie_id
AND ci.role_id = rt.id
AND n.id = pi.person_id
AND pi.info_type_id = it2.id
AND (it1.id IN ID1)
AND (it2.id IN ID2)
AND (mi1.info IN INFO)
AND (n.name ILIKE NAME)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
GROUP BY mi1.info, n.name""",
    "Q10a": """SELECT n.name, mi1.info, MIN(t.production_year), MAX(t.production_year)
FROM title as t,
kind_type as kt,
movie_info as mi1,
info_type as it1,
cast_info as ci,
role_type as rt,
name as n
WHERE
t.id = ci.movie_id
AND t.id = mi1.movie_id
AND mi1.info_type_id = it1.id
AND t.kind_id = kt.id
AND ci.person_id = n.id
AND ci.movie_id = mi1.movie_id
AND ci.role_id = rt.id
AND (it1.id IN ID)
AND (mi1.info IN INFO)
AND (n.name ILIKE NAME)
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
GROUP BY mi1.info, n.name""",
    "Q11a": """SELECT n.gender, rt.role, cn.name, COUNT(*)
FROM title as t,
movie_companies as mc,
company_name as cn,
company_type as ct,
kind_type as kt,
cast_info as ci,
name as n,
role_type as rt,
movie_info as mi1,
info_type as it
WHERE t.id = mc.movie_id
AND t.id = ci.movie_id
AND t.id = mi1.movie_id
AND mi1.movie_id = ci.movie_id
AND ci.movie_id = mc.movie_id
AND cn.id = mc.company_id
AND ct.id = mc.company_type_id
AND kt.id = t.kind_id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND mi1.info_type_id = it.id
AND (kt.kind ILIKE KIND)
AND (rt.role IN ROLE)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (it.id IN ID)
AND (mi1.info ILIKE INFO)
AND (cn.name ILIKE NAME)
GROUP BY n.gender, rt.role, cn.name
ORDER BY COUNT(*) DESC""",
    "Q11b": """SELECT n.gender, rt.role, cn.name, COUNT(*)
FROM title as t,
movie_companies as mc,
company_name as cn,
company_type as ct,
kind_type as kt,
cast_info as ci,
name as n,
role_type as rt,
movie_info as mi1,
info_type as it1,
person_info as pi,
info_type as it2
WHERE t.id = mc.movie_id
AND t.id = ci.movie_id
AND t.id = mi1.movie_id
AND mi1.movie_id = ci.movie_id
AND ci.movie_id = mc.movie_id
AND cn.id = mc.company_id
AND ct.id = mc.company_type_id
AND kt.id = t.kind_id
AND ci.person_id = n.id
AND ci.role_id = rt.id
AND mi1.info_type_id = it1.id
AND n.id = pi.person_id
AND pi.info_type_id = it2.id
AND ci.person_id = pi.person_id
AND (kt.kind IN KIND)
AND (rt.role IN ROLE)
AND (t.production_year <= YEAR1)
AND (t.production_year >= YEAR2)
AND (it1.id IN ID1)
AND (mi1.info ILIKE INFO1)
AND (pi.info ILIKE INFO2)
AND (it2.id IN ID2)
GROUP BY n.gender, rt.role, cn.name
ORDER BY COUNT(*) DESC""",
}
