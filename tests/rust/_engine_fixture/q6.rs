//! Query 6 (test fixture).
use engine_builder::Database;
use synno_rt::prelude::*;
use crate::args::Q6Args;
use crate::dates::{days_from_civil, plus_one_year};

pub fn run_q6(db: &Database, args: &Q6Args) -> Result<RecordBatch> {
    let (y,m,d) = parse_ymd(&args.DATE)?;
    let lo_date = days_from_civil(y,m,d); let hi_date = plus_one_year(y,m,d);
    let disc: f64 = args.DISCOUNT.trim().parse().map_err(|_| Error::new(format!("Q6: bad DISCOUNT: {:?}", args.DISCOUNT)))?;
    let disc_s2 = (disc*100.0).round() as i64; let (disc_lo,disc_hi)=(disc_s2-1,disc_s2+1);
    let qty: f64 = args.QUANTITY.trim().parse().map_err(|_| Error::new(format!("Q6: bad QUANTITY: {:?}", args.QUANTITY)))?;
    let qty_s2 = (qty*100.0).round() as i64;
    let n_rows = db.rows(); const MORSEL: usize = 1<<16; let n_morsels = n_rows.div_ceil(MORSEL);
    let revenue: i128 = parallel_reduce(n_morsels, 0i128,
        |mut local, mo| {
            let lo=mo*MORSEL; let hi=(lo+MORSEL).min(n_rows);
            for row in lo..hi {
                let sd=db.l_shipdate[row]; if sd<lo_date || sd>=hi_date { continue; }
                let dsc=db.l_discount[row]; if dsc<disc_lo || dsc>disc_hi { continue; }
                if db.l_quantity[row]>=qty_s2 { continue; }
                local += db.l_extendedprice[row] as i128 * dsc as i128;
            }
            local
        }, |a,b| a+b);
    let nn: Validity = Vec::new();
    make_table(vec![("revenue", decimal_column(&[revenue], 38, 4, &nn)?)])
}
fn parse_ymd(s: &str) -> Result<(i32,u32,u32)> {
    let s=s.trim(); let mut it=s.split('-');
    let bad=|| Error::new(format!("Q6: bad DATE: {s:?}"));
    let y=it.next().ok_or_else(bad)?.parse().map_err(|_| bad())?;
    let m=it.next().ok_or_else(bad)?.parse().map_err(|_| bad())?;
    let d=it.next().ok_or_else(bad)?.parse().map_err(|_| bad())?;
    Ok((y,m,d))
}
