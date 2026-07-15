//! Hand-written reference engine for TPC-H Q1 + Q6 (test fixture).
use engine_loader::ParquetTables;
use synno_rt::prelude::*;

pub struct Database {
    pub l_shipdate: Vec<i32>,
    pub l_quantity: Vec<i64>,
    pub l_extendedprice: Vec<i64>,
    pub l_discount: Vec<i64>,
    pub l_tax: Vec<i64>,
    pub l_returnflag: Vec<u8>,
    pub l_linestatus: Vec<u8>,
}
impl Database { pub fn rows(&self) -> usize { self.l_shipdate.len() } }

pub fn build(tables: &ParquetTables) -> Result<Database> {
    let li = &tables.lineitem;
    let first_byte = |v: Vec<String>| -> Vec<u8> {
        v.into_iter().map(|s| s.as_bytes().first().copied().unwrap_or(b' ')).collect()
    };
    Ok(Database {
        l_shipdate: as_date_days(li, "l_shipdate")?,
        l_quantity: scaled_integer::<i64>(li, "l_quantity", 2)?,
        l_extendedprice: scaled_integer::<i64>(li, "l_extendedprice", 2)?,
        l_discount: scaled_integer::<i64>(li, "l_discount", 2)?,
        l_tax: scaled_integer::<i64>(li, "l_tax", 2)?,
        l_returnflag: first_byte(as_string(li, "l_returnflag")?),
        l_linestatus: first_byte(as_string(li, "l_linestatus")?),
    })
}
