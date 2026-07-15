//! Query 1 (test fixture).
use engine_builder::Database;
use synno_rt::prelude::*;
use crate::args::Q1Args;
use crate::dates::days_from_civil;

const NGROUPS: usize = 26 * 26;
#[derive(Clone, Copy)]
struct Group { sum_qty: i128, sum_base: i128, sum_disc: i128, sum_charge: i128, sum_disc_raw: i128, count: i64 }
impl Default for Group { fn default() -> Self { Group { sum_qty:0, sum_base:0, sum_disc:0, sum_charge:0, sum_disc_raw:0, count:0 } } }
#[derive(Clone)]
struct Acc(Vec<Group>);
impl Default for Acc { fn default() -> Self { Acc(vec![Group::default(); NGROUPS]) } }

pub fn run_q1(db: &Database, args: &Q1Args) -> Result<RecordBatch> {
    let delta: i64 = args.DELTA.trim().parse().map_err(|_| Error::new(format!("Q1: bad DELTA: {:?}", args.DELTA)))?;
    let cutoff = days_from_civil(1998, 12, 1) - delta as i32;
    let n_rows = db.rows();
    const MORSEL: usize = 1 << 16;
    let n_morsels = n_rows.div_ceil(MORSEL);
    let acc = parallel_reduce(n_morsels, Acc::default(),
        |mut local, m| {
            let lo = m * MORSEL; let hi = (lo + MORSEL).min(n_rows);
            for row in lo..hi {
                if db.l_shipdate[row] > cutoff { continue; }
                let g = &mut local.0[(db.l_returnflag[row] - b'A') as usize * 26 + (db.l_linestatus[row] - b'A') as usize];
                let qty = db.l_quantity[row] as i128; let price = db.l_extendedprice[row] as i128;
                let disc = db.l_discount[row] as i128; let tax = db.l_tax[row] as i128;
                let disc_price = price * (100 - disc);
                g.sum_qty += qty; g.sum_base += price; g.sum_disc += disc_price;
                g.sum_charge += disc_price * (100 + tax); g.sum_disc_raw += disc; g.count += 1;
            }
            local
        },
        |mut a, b| { for (ga, gb) in a.0.iter_mut().zip(b.0.iter()) {
            ga.sum_qty += gb.sum_qty; ga.sum_base += gb.sum_base; ga.sum_disc += gb.sum_disc;
            ga.sum_charge += gb.sum_charge; ga.sum_disc_raw += gb.sum_disc_raw; ga.count += gb.count;
        } a });
    let mut l_returnflag=Vec::new(); let mut l_linestatus=Vec::new();
    let (mut sum_qty, mut sum_base_price)=(Vec::new(),Vec::new());
    let (mut sum_disc_price, mut sum_charge)=(Vec::new(),Vec::new());
    let (mut avg_qty, mut avg_price, mut avg_disc)=(Vec::new(),Vec::new(),Vec::new());
    let mut count_order=Vec::new();
    for i in 0..NGROUPS {
        let g = acc.0[i]; if g.count == 0 { continue; }
        l_returnflag.push((((i/26) as u8 + b'A') as char).to_string());
        l_linestatus.push((((i%26) as u8 + b'A') as char).to_string());
        sum_qty.push(g.sum_qty); sum_base_price.push(g.sum_base);
        sum_disc_price.push(g.sum_disc); sum_charge.push(g.sum_charge);
        let c = g.count as f64;
        avg_qty.push(g.sum_qty as f64/100.0/c); avg_price.push(g.sum_base as f64/100.0/c);
        avg_disc.push(g.sum_disc_raw as f64/100.0/c); count_order.push(g.count);
    }
    let nn: Validity = Vec::new();
    make_table(vec![
        ("l_returnflag", string_column(&l_returnflag, &nn, None)?),
        ("l_linestatus", string_column(&l_linestatus, &nn, None)?),
        ("sum_qty", decimal_column(&sum_qty, 38, 2, &nn)?),
        ("sum_base_price", decimal_column(&sum_base_price, 38, 2, &nn)?),
        ("sum_disc_price", decimal_column(&sum_disc_price, 38, 4, &nn)?),
        ("sum_charge", decimal_column(&sum_charge, 38, 6, &nn)?),
        ("avg_qty", double_column(&avg_qty, &nn, None)?),
        ("avg_price", double_column(&avg_price, &nn, None)?),
        ("avg_disc", double_column(&avg_disc, &nn, None)?),
        ("count_order", int64_column(&count_order, &nn, None)?),
    ])
}
