#include "read_api.hpp"
#include "bff_format.hpp"
#include "filter_pushdown.hpp"

#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>

// FILE_VERSION: 2

// ============================================================
// Footer deserialization
// ============================================================

static CsfTableFooter deserialize_table_footer(const uint8_t* bytes, size_t size,
                                                const std::string& table_name) {
    CsfTableFooter ft;
    ft.table_name = table_name;
    ByteReader br(bytes, size);

    // Read TOC header
    uint32_t section_count = br.read_u32();
    if (section_count != FOOTER_SECT_COUNT)
        throw std::runtime_error("Footer section count mismatch");

    struct TocEntry { uint8_t id; uint8_t pad[3]; uint32_t offset; uint32_t length; };
    TocEntry toc[FOOTER_SECT_COUNT];
    for (uint32_t i = 0; i < section_count; i++) {
        toc[i].id = br.read_u8();
        toc[i].pad[0] = br.read_u8(); toc[i].pad[1] = br.read_u8(); toc[i].pad[2] = br.read_u8();
        toc[i].offset = br.read_u32();
        toc[i].length = br.read_u32();
    }

    // Helper to make a reader for a section
    auto section_reader = [&](int sect) -> ByteReader {
        return ByteReader(bytes + toc[sect].offset, toc[sect].length);
    };

    // SCHEMA section
    {
        ByteReader r = section_reader(FOOTER_SECT_SCHEMA);
        uint32_t ncols = r.read_u32();
        ft.num_cols = ncols;
        ft.cols.resize(ncols);
        for (uint32_t i = 0; i < ncols; i++) {
            auto& m = ft.cols[i];
            m.name        = r.read_str();
            m.encoding    = CsfEncoding(r.read_u8());
            m.phys_bytes  = r.read_u8();
            m.bit_width   = r.read_u8();
            m.dict_id     = r.read_u8();
            m.is_signed   = r.read_u8() != 0;
            m.scale       = int32_t(r.read_u32());
            m.date_epoch  = int32_t(r.read_u32());
            m.nullable    = r.read_u8() != 0;
            m.synthetic_prefix = r.read_u8() != 0;
            m.prefix_str  = r.read_str();
            m.split_phone = r.read_u8() != 0;
            m.bff_phys    = BffPhysicalType(r.read_u8());
        }
    }

    // DICTS section
    {
        ByteReader r = section_reader(FOOTER_SECT_DICTS);
        uint32_t ndict = r.read_u32();
        ft.dicts.resize(ndict);
        for (uint32_t i = 0; i < ndict; i++) {
            auto& d = ft.dicts[i];
            d.dict_id  = r.read_u8();
            d.col_name = r.read_str();
            uint32_t ne = r.read_u32();
            d.entries.resize(ne);
            for (uint32_t j = 0; j < ne; j++) d.entries[j] = r.read_str();
        }
    }

    // SEG_OFFSETS section
    {
        ByteReader r = section_reader(FOOTER_SECT_SEG_OFFSETS);
        ft.num_rows     = r.read_u64();
        ft.num_segments = r.read_u32();
        uint32_t ncols  = r.read_u32(); // should match ft.num_cols
        ft.seg_file_offsets.resize(ft.num_segments);
        ft.col_block_offsets.resize(uint64_t(ft.num_segments) * ft.num_cols);
        for (uint32_t s = 0; s < ft.num_segments; s++) {
            ft.seg_file_offsets[s] = r.read_u64();
            for (uint32_t c = 0; c < ft.num_cols; c++) {
                ft.col_block_offsets[s * ft.num_cols + c] = r.read_u64();
            }
        }
    }

    // ZONE_MAPS section
    {
        ByteReader r = section_reader(FOOTER_SECT_ZONE_MAPS);
        ft.zone_maps.resize(ft.num_segments);
        for (uint32_t s = 0; s < ft.num_segments; s++) {
            auto& zm = ft.zone_maps[s];
            zm.col_min.resize(ft.num_cols);
            zm.col_max.resize(ft.num_cols);
            zm.col_bitset.resize(ft.num_cols);
            zm.col_null_count.resize(ft.num_cols);
            for (uint32_t c = 0; c < ft.num_cols; c++) {
                zm.col_min[c]        = r.read_i64();
                zm.col_max[c]        = r.read_i64();
                zm.col_bitset[c]     = r.read_u64();
                zm.col_null_count[c] = r.read_u64();
            }
        }
    }

    // BLOOM section
    {
        ByteReader r = section_reader(FOOTER_SECT_BLOOM);
        for (uint32_t s = 0; s < ft.num_segments; s++) {
            uint32_t bloom_size = r.read_u32();
            ft.zone_maps[s].bloom.resize(bloom_size);
            if (bloom_size > 0)
                r.read_bytes(ft.zone_maps[s].bloom.data(), bloom_size);
        }
    }

    return ft;
}

// ============================================================
// BffTableInfo builder from CsfTableFooter
// ============================================================
static BffTableInfo make_bff_table_info(const CsfTableFooter& ft,
                                         const std::string& file_path) {
    BffTableInfo info;
    info.name      = ft.table_name;
    info.path      = file_path;
    info.row_count = ft.num_rows;
    for (uint32_t ci = 0; ci < ft.num_cols; ci++) {
        BffColumnInfo c;
        c.id            = ci;
        c.name          = ft.cols[ci].name;
        c.physical_type = ft.cols[ci].bff_phys;
        c.nullable      = ft.cols[ci].nullable;
        info.columns.push_back(c);
    }
    for (uint32_t si = 0; si < ft.num_segments; si++) {
        BffRowGroupInfo rg;
        rg.id          = si;
        rg.row_start   = uint64_t(si) * CSF_SEGMENT_ROWS;
        rg.row_count   = (si + 1 < ft.num_segments) ? CSF_SEGMENT_ROWS :
                         ft.num_rows - uint64_t(si) * CSF_SEGMENT_ROWS;
        rg.file_offset = ft.seg_file_offsets[si];
        for (uint32_t ci = 0; ci < ft.num_cols; ci++) {
            BffColumnStats cs;
            cs.has_min = true; cs.has_max = true;
            cs.null_count = ft.zone_maps[si].col_null_count[ci];
            int64_t mn = ft.zone_maps[si].col_min[ci];
            int64_t mx = ft.zone_maps[si].col_max[ci];
            cs.min_value.resize(8); memcpy(cs.min_value.data(), &mn, 8);
            cs.max_value.resize(8); memcpy(cs.max_value.data(), &mx, 8);
            rg.column_stats.push_back(cs);
        }
        info.row_groups.push_back(rg);
    }
    return info;
}

// ============================================================
// Open / close dataset
// ============================================================

BffDataset* open_bff_dataset(std::string bff_dir, const BffOpenOptions& options) {
    auto* dataset = new BffDataset();
    dataset->root_path = std::move(bff_dir);
    dataset->options   = options;
    return dataset;
}

void close_bff_dataset(BffDataset* dataset) {
    delete dataset;
}

// ============================================================
// Database handle
// ============================================================

Database* build_bff_query_database(std::string bff_dir, const BffOpenOptions& options) {
    auto* db    = new Database();
    db->dataset = open_bff_dataset(std::move(bff_dir), options);
    db->footer  = load_bff_footer(db->dataset, /*refresh_cache=*/false);
    return db;
}

void destroy_bff_query_database(Database* db) {
    if (!db) return;
    close_bff_dataset(db->dataset);
    delete db;
}

// ============================================================
// Footer loading
// ============================================================

BffFooter* load_bff_footer(BffDataset* dataset, bool refresh_cache) {
    if (!dataset) return nullptr;
    if (dataset->has_footer && !refresh_cache) return &dataset->footer;

    dataset->footer.info.root_path = dataset->root_path;
    dataset->footer.csf_tables.clear();
    dataset->footer.info.tables.clear();

    static const char* TABLE_NAMES[] = {
        "lineitem", "orders", "customer", "part", "supplier", "nation", "region", "partsupp"
    };
    for (const char* tname : TABLE_NAMES) {
        std::string path = dataset->root_path + "/" + std::string(tname) + ".csf";

        FILE* fp = fopen(path.c_str(), "rb");
        if (!fp) continue; // table file not present

        CsfFileHeader fhdr;
        if (fread(&fhdr, 1, sizeof(fhdr), fp) != sizeof(fhdr)) { fclose(fp); continue; }
        // Validate magic
        if (memcmp(fhdr.magic, "CSFv0001", 8) != 0) { fclose(fp); continue; }

        // Read footer bytes
        fseek(fp, 0, SEEK_END);
        long fsize = ftell(fp);
        if (fsize < 0 || fhdr.footer_offset + fhdr.footer_length > (uint64_t)fsize) {
            fclose(fp); continue;
        }
        fseek(fp, (long)fhdr.footer_offset, SEEK_SET);
        std::vector<uint8_t> footer_bytes(fhdr.footer_length);
        if (fread(footer_bytes.data(), 1, fhdr.footer_length, fp) != fhdr.footer_length) {
            fclose(fp); continue;
        }
        fclose(fp);

        try {
            CsfTableFooter ft = deserialize_table_footer(
                footer_bytes.data(), footer_bytes.size(), tname);
            dataset->footer.info.tables.push_back(make_bff_table_info(ft, path));
            dataset->footer.csf_tables[tname] = std::move(ft);
        } catch (...) {
            // Skip malformed tables
        }
    }

    dataset->has_footer = true;
    return &dataset->footer;
}

const BffFooter* cached_bff_footer(BffDataset* dataset) {
    if (!dataset || !dataset->has_footer) return nullptr;
    return &dataset->footer;
}

BffFooterInfo describe_bff_footer(const BffFooter* footer) {
    if (!footer) return {};
    return footer->info;
}

void invalidate_bff_footer_cache(BffDataset* dataset) {
    if (dataset) dataset->has_footer = false;
}

// ============================================================
// Table open / close
// ============================================================

BffTable* open_bff_table(BffDataset* dataset, std::string table_name) {
    auto* tbl = new BffTable();
    tbl->dataset    = dataset;
    tbl->table_name = table_name;
    if (dataset) {
        tbl->file_path  = dataset->root_path + "/" + table_name + ".csf";
        // Find csf_footer
        auto it = dataset->footer.csf_tables.find(table_name);
        if (it != dataset->footer.csf_tables.end()) {
            tbl->csf_footer = &it->second;
            // Build BffTableInfo
            tbl->info = make_bff_table_info(it->second, tbl->file_path);
        } else {
            tbl->info.name = table_name;
            tbl->info.path = dataset->root_path;
        }
        // Open file descriptor for reads
        tbl->fd = open(tbl->file_path.c_str(), O_RDONLY);
    }
    return tbl;
}

void close_bff_table(BffTable* tbl) {
    if (tbl) {
        if (tbl->fd >= 0) { ::close(tbl->fd); tbl->fd = -1; }
        delete tbl;
    }
}

BffTableInfo describe_bff_table(const BffTable* tbl) {
    if (!tbl) return {};
    return tbl->info;
}

BffRowGroupInfo describe_bff_row_group(BffTable* tbl, std::uint32_t rg_id) {
    if (tbl && rg_id < tbl->info.row_groups.size())
        return tbl->info.row_groups[rg_id];
    return {};
}

BffPageInfo describe_bff_page(BffTable* tbl, uint32_t rg_id, uint32_t col_id, uint32_t page_id) {
    BffPageInfo info;
    info.row_group_id = rg_id;
    info.column_id    = col_id;
    info.page_id      = page_id;
    if (tbl && tbl->csf_footer && rg_id < tbl->csf_footer->num_segments &&
        col_id < tbl->csf_footer->num_cols) {
        const auto& ft = *tbl->csf_footer;
        info.file_offset = ft.seg_file_offsets[rg_id] +
                           ft.col_block_offsets[rg_id * ft.num_cols + col_id];
        info.row_count   = (rg_id + 1 < ft.num_segments) ? CSF_SEGMENT_ROWS :
                           ft.num_rows - uint64_t(rg_id) * CSF_SEGMENT_ROWS;
    }
    return info;
}

// ============================================================
// Scan plan (zone-map pruning)
// ============================================================

// Evaluate a filter node against the zone map of one segment.
// Returns BffPruneDecision::Drop if the segment can definitely be skipped.
static BffPruneDecision eval_filter_node(
        const BffFilterNode& node,
        const std::vector<BffFilterNode>& nodes,
        const CsfSegZoneMap& zm,
        const CsfTableFooter& ft) {
    switch (node.kind) {
        case BffFilterNodeKind::AlwaysTrue:  return BffPruneDecision::Keep;
        case BffFilterNodeKind::AlwaysFalse: return BffPruneDecision::Drop;
        case BffFilterNodeKind::Predicate: {
            uint32_t col_id = node.predicate.column_id;
            if (col_id >= ft.num_cols) return BffPruneDecision::Keep;
            int64_t cmin = zm.col_min[col_id];
            int64_t cmax = zm.col_max[col_id];
            uint64_t cbs = zm.col_bitset[col_id];
            if (cmin > cmax) return BffPruneDecision::Keep; // no data

            auto get_lit_i64 = [](const BffLiteral& lit) -> int64_t {
                if (lit.value.size() >= 8) {
                    int64_t v; memcpy(&v, lit.value.data(), 8); return v;
                }
                return 0;
            };

            switch (node.predicate.op) {
                case BffPredicateOp::LessEqual:
                    if (!node.predicate.values.empty()) {
                        int64_t threshold = get_lit_i64(node.predicate.values[0]);
                        if (cmin > threshold) return BffPruneDecision::Drop;
                    }
                    break;
                case BffPredicateOp::LessThan:
                    if (!node.predicate.values.empty()) {
                        int64_t threshold = get_lit_i64(node.predicate.values[0]);
                        if (cmin >= threshold) return BffPruneDecision::Drop;
                    }
                    break;
                case BffPredicateOp::GreaterEqual:
                    if (!node.predicate.values.empty()) {
                        int64_t threshold = get_lit_i64(node.predicate.values[0]);
                        if (cmax < threshold) return BffPruneDecision::Drop;
                    }
                    break;
                case BffPredicateOp::GreaterThan:
                    if (!node.predicate.values.empty()) {
                        int64_t threshold = get_lit_i64(node.predicate.values[0]);
                        if (cmax <= threshold) return BffPruneDecision::Drop;
                    }
                    break;
                case BffPredicateOp::Equal:
                    if (!node.predicate.values.empty()) {
                        int64_t threshold = get_lit_i64(node.predicate.values[0]);
                        if (cmax < threshold || cmin > threshold) return BffPruneDecision::Drop;
                        // Bitset check for dict columns
                        if (cbs != 0 && threshold >= 0 && threshold < 64) {
                            if (!(cbs & (1ULL << threshold))) return BffPruneDecision::Drop;
                        }
                    }
                    break;
                case BffPredicateOp::InList: {
                    bool any_possible = false;
                    for (const auto& lit : node.predicate.values) {
                        int64_t v = get_lit_i64(lit);
                        if (v >= cmin && v <= cmax) {
                            if (cbs == 0 || (v >= 0 && v < 64 && (cbs & (1ULL << v)))) {
                                any_possible = true;
                                break;
                            }
                        }
                    }
                    if (!any_possible) return BffPruneDecision::Drop;
                    break;
                }
                default: break;
            }
            return BffPruneDecision::Keep;
        }
        case BffFilterNodeKind::And: {
            for (uint32_t i = 0; i < node.child_count; i++) {
                uint32_t ci = node.first_child + i;
                if (ci >= nodes.size()) break;
                auto r = eval_filter_node(nodes[ci], nodes, zm, ft);
                if (r == BffPruneDecision::Drop) return BffPruneDecision::Drop;
            }
            return BffPruneDecision::Keep;
        }
        case BffFilterNodeKind::Or: {
            bool any_keep = false;
            for (uint32_t i = 0; i < node.child_count; i++) {
                uint32_t ci = node.first_child + i;
                if (ci >= nodes.size()) break;
                auto r = eval_filter_node(nodes[ci], nodes, zm, ft);
                if (r != BffPruneDecision::Drop) { any_keep = true; break; }
            }
            return any_keep ? BffPruneDecision::Keep : BffPruneDecision::Drop;
        }
        case BffFilterNodeKind::Not: {
            // Don't invert for pruning (conservative)
            return BffPruneDecision::Keep;
        }
    }
    return BffPruneDecision::Keep;
}

BffScanPlan plan_bff_scan(BffTable* tbl, const BffScanRequest& request) {
    BffScanPlan plan;
    if (!tbl || !tbl->csf_footer) return plan;
    const auto& ft = *tbl->csf_footer;

    for (uint32_t seg = 0; seg < ft.num_segments; seg++) {
        BffPruneDecision decision = BffPruneDecision::Keep;
        if (request.enable_row_group_pruning && !request.filter.nodes.empty()) {
            uint32_t root = request.filter.root_node;
            if (root < request.filter.nodes.size()) {
                decision = eval_filter_node(
                    request.filter.nodes[root],
                    request.filter.nodes,
                    ft.zone_maps[seg], ft);
            }
        }
        if (decision != BffPruneDecision::Drop) {
            plan.row_group_ids.push_back(seg);
            plan.estimated_rows += (seg + 1 < ft.num_segments) ? CSF_SEGMENT_ROWS :
                ft.num_rows - uint64_t(seg) * CSF_SEGMENT_ROWS;
        } else {
            plan.pruned_row_groups++;
        }
    }
    return plan;
}

// ============================================================
// Buffer reads
// ============================================================

BffBuffer* read_bff_row_group(
        BffTable* tbl,
        uint32_t rg_id,
        const BffColumnSelection& /*columns*/,
        const BffReadOptions& /*options*/) {
    if (!tbl || !tbl->csf_footer || tbl->fd < 0) return new BffBuffer();
    const auto& ft = *tbl->csf_footer;
    if (rg_id >= ft.num_segments) return new BffBuffer();

    uint64_t seg_start = ft.seg_file_offsets[rg_id];
    uint64_t seg_end;
    if (rg_id + 1 < ft.num_segments) {
        seg_end = ft.seg_file_offsets[rg_id + 1];
    } else {
        // Segment ends at footer
        // We stored footer_offset in the file header
        CsfFileHeader fhdr;
        (void)pread(tbl->fd, &fhdr, sizeof(fhdr), 0);
        seg_end = fhdr.footer_offset;
    }
    if (seg_end <= seg_start) return new BffBuffer();

    uint64_t seg_size = seg_end - seg_start;
    auto* data = new uint8_t[seg_size];
    (void)pread(tbl->fd, data, seg_size, (off_t)seg_start);

    auto* buf = new BffBuffer();
    buf->data        = data;
    buf->size        = seg_size;
    buf->file_offset = seg_start;
    buf->storage     = BffBufferStorage::Owned;
    buf->encoded     = true;
    buf->owner       = data;
    return buf;
}

BffBuffer* read_bff_page(
        BffTable* tbl,
        uint32_t rg_id,
        uint32_t col_id,
        uint32_t /*page_id*/,
        const BffReadOptions& options) {
    if (!tbl || !tbl->csf_footer || tbl->fd < 0) return new BffBuffer();
    const auto& ft = *tbl->csf_footer;
    if (rg_id >= ft.num_segments || col_id >= ft.num_cols) return new BffBuffer();

    uint64_t seg_start = ft.seg_file_offsets[rg_id];
    uint64_t col_off   = ft.col_block_offsets[rg_id * ft.num_cols + col_id];
    uint64_t block_off = seg_start + col_off;

    // Read block header
    CsfBlockHeader hdr;
    ssize_t hr = pread(tbl->fd, &hdr, sizeof(hdr), (off_t)block_off);
    if (hr != sizeof(hdr) || hdr.magic != CSF_BLOCK_MAGIC) return new BffBuffer();

    uint64_t payload_off = block_off + sizeof(hdr);
    // Skip encoding sub-header
    switch (CsfEncoding(hdr.encoding)) {
        case CsfEncoding::FOR_BITPACK:    payload_off += sizeof(CsfForHeader);   break;
        case CsfEncoding::DELTA_BITPACK:  payload_off += sizeof(CsfDeltaHeader); break;
        case CsfEncoding::DICT_BITPACK:   payload_off += sizeof(CsfDictHeader);  break;
        case CsfEncoding::STRING_RAW:     payload_off += sizeof(CsfStringHeader);break;
        default: break;
    }

    if (!options.decompress) {
        // Return compressed bytes only (header + subheader + payload)
        uint64_t total = sizeof(hdr);
        switch (CsfEncoding(hdr.encoding)) {
            case CsfEncoding::FOR_BITPACK:    total += sizeof(CsfForHeader);   break;
            case CsfEncoding::DELTA_BITPACK:  total += sizeof(CsfDeltaHeader); break;
            case CsfEncoding::DICT_BITPACK:   total += sizeof(CsfDictHeader);  break;
            case CsfEncoding::STRING_RAW:     total += sizeof(CsfStringHeader);break;
            default: break;
        }
        total += hdr.compressed_bytes;
        auto* data = new uint8_t[total];
        (void)pread(tbl->fd, data, total, (off_t)block_off);
        auto* buf = new BffBuffer();
        buf->data = data; buf->size = total;
        buf->file_offset = block_off;
        buf->storage = BffBufferStorage::Owned;
        buf->encoded = true; buf->owner = data;
        return buf;
    }

    // Read and decompress
    // First, determine the full block size including sub-header
    uint64_t sub_hdr_size = 0;
    CsfForHeader    fh{};
    CsfDeltaHeader  dh{};
    CsfDictHeader   dicth{};
    CsfStringHeader sh{};
    switch (CsfEncoding(hdr.encoding)) {
        case CsfEncoding::FOR_BITPACK:
            (void)pread(tbl->fd, &fh, sizeof(fh), (off_t)(block_off + sizeof(hdr)));
            sub_hdr_size = sizeof(fh); break;
        case CsfEncoding::DELTA_BITPACK:
            (void)pread(tbl->fd, &dh, sizeof(dh), (off_t)(block_off + sizeof(hdr)));
            sub_hdr_size = sizeof(dh); break;
        case CsfEncoding::DICT_BITPACK:
            (void)pread(tbl->fd, &dicth, sizeof(dicth), (off_t)(block_off + sizeof(hdr)));
            sub_hdr_size = sizeof(dicth); break;
        case CsfEncoding::STRING_RAW:
            (void)pread(tbl->fd, &sh, sizeof(sh), (off_t)(block_off + sizeof(hdr)));
            sub_hdr_size = sizeof(sh); break;
        default: break;
    }

    uint64_t compressed_start = block_off + sizeof(hdr) + sub_hdr_size;
    std::vector<uint8_t> compressed(hdr.compressed_bytes);
    (void)pread(tbl->fd, compressed.data(), hdr.compressed_bytes, (off_t)compressed_start);

    // Decompress (currently identity: compressed == raw)
    auto* raw = new uint8_t[hdr.raw_bytes + 1];
    if (hdr.raw_bytes > 0) {
        uint32_t copy_size = std::min(hdr.compressed_bytes, hdr.raw_bytes);
        memcpy(raw, compressed.data(), copy_size);
    }

    // Pack the decoded result: include a small header so callers know meta info.
    // We return raw bytes plus a prepended CsfBlockHeader so callers can parse.
    // The BffBuffer::encoded=false means the raw decoded payload is in data[].
    auto* buf = new BffBuffer();
    buf->data = raw; buf->size = hdr.raw_bytes;
    buf->file_offset = block_off;
    buf->storage = BffBufferStorage::Owned;
    buf->encoded = false; buf->owner = raw;
    return buf;
}

void release_bff_buffer(BffBuffer* buffer) {
    if (!buffer) return;
    if (buffer->storage == BffBufferStorage::Owned && buffer->data) {
        delete[] buffer->data;
    }
    delete buffer;
}
