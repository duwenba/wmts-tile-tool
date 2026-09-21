//! 分块 BigTIFF 写入器（无损 deflate 压缩，逐瓦片并行）。
//!
//! 内存模型：一次只保留一个 64 瓦片批次（并行压缩），写盘后立即释放。
//! 峰值内存与整图大小无关，只与单批有关（几十 MB 以内）。
//!
//! 布局：
//!   BigTIFF 头(12B) | 瓦片数据(逐个 deflate 流) | IFD | 附加数据区
//!   瓦片按行优先顺序 (tile_row, tile_col) 排列，与源瓦片 1:1 对应。

use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};

use libdeflater::{CompressionLvl, Compressor};
use rayon::prelude::*;

/// 并行压缩批次大小（瓦片数）
const CHUNK: usize = 64;

const TAG_IMAGE_WIDTH: u16 = 256;
const TAG_IMAGE_LENGTH: u16 = 257;
const TAG_BITS_PER_SAMPLE: u16 = 258;
const TAG_COMPRESSION: u16 = 259;
const TAG_PHOTOMETRIC: u16 = 262;
const TAG_TILE_OFFSETS: u16 = 273;
const TAG_SAMPLES_PER_PIXEL: u16 = 277;
const TAG_TILE_BYTE_COUNTS: u16 = 279;
const TAG_PLANAR_CONFIG: u16 = 284;
const TAG_TILE_WIDTH: u16 = 322;
const TAG_TILE_LENGTH: u16 = 323;
const TAG_SAMPLE_FORMAT: u16 = 339;

const TYPE_SHORT: u16 = 3;
const TYPE_LONG: u16 = 4;
const TYPE_LONG8: u16 = 16;

const COMPRESSION_DEFLATE: u64 = 8; // Adobe deflate
const PHOTOMETRIC_RGB: u64 = 2;

/// 写入分块 BigTIFF。
///
/// `get_tile(tc, tr)` 返回第 (tc, tr) 块瓦片的 RGB8 像素（w*h*3 字节），
/// 返回 None 时用纯黑填充（与 Python 版缺失瓦片行为一致）。
/// `progress` 收到已写瓦片数。
pub fn write_bigtiff<F>(
    path: &Path,
    width: u32,
    height: u32,
    tile_w: u32,
    tile_h: u32,
    level: u8,
    progress: &dyn Fn(usize, usize),
    get_tile: F,
) -> std::io::Result<()>
where
    F: Fn(u32, u32) -> Option<Vec<u8>> + Sync,
{
    let ncols = width.div_ceil(tile_w) as usize;
    let nrows = height.div_ceil(tile_h) as usize;
    let ntiles = ncols * nrows;
    let tile_bytes = (tile_w * tile_h * 3) as usize;

    let mut f = BufWriter::with_capacity(1 << 20, File::create(path)?);

    // ---- BigTIFF 头（16 字节）----
    // 字节 0-1: "II"；2-3: magic=43；4-5: offsetsize=8；6-7: extra=0；
    // 8-15: 首个 IFD 的 64 位偏移（先占位，最后回填）
    f.write_all(b"II")?;
    f.write_all(&43u16.to_le_bytes())?;
    f.write_all(&8u16.to_le_bytes())?;
    f.write_all(&0u16.to_le_bytes())?;
    f.write_all(&0u64.to_le_bytes())?; // 占位: IFD 偏移

    // ---- 瓦片数据：按行优先分块并行压缩、顺序写盘 ----
    // 并行只做“解压→压缩”，写盘严格串行（偏移必须连续），
    // 因此偏移/字节数用普通 Vec 顺序 push 即可。
    let mut offsets: Vec<u64> = Vec::with_capacity(ntiles);
    let mut counts: Vec<u64> = Vec::with_capacity(ntiles);
    let done = AtomicUsize::new(0);

    let mut t = 0usize;
    while t < ntiles {
        let end = (t + CHUNK).min(ntiles);
        // 并行：读瓦片 → RGB → deflate；批次内结果顺序不变
        let chunk_data: Vec<Vec<u8>> = (t..end)
            .into_par_iter()
            .map(|ti| {
                let (tr, tc) = (ti / ncols, ti % ncols);
                let rgb = match get_tile(tc as u32, tr as u32) {
                    Some(r) if r.len() == tile_bytes => r,
                    _ => vec![0u8; tile_bytes], // 缺失/尺寸异常 → 黑
                };
                deflate(&rgb, level)
            })
            .collect();

        for data in chunk_data {
            let off = f.stream_position()?;
            f.write_all(&data)?;
            offsets.push(off);
            counts.push(data.len() as u64);
        }

        t = end;
        done.store(t, Ordering::Relaxed);
        progress(t, ntiles);

        // 定期落盘，避免 BufWriter 一直兜着太多数据
        if done.load(Ordering::Relaxed) % (CHUNK * 16) == 0 {
            f.flush()?;
        }
    }

    debug_assert_eq!(offsets.len(), ntiles);
    debug_assert_eq!(counts.len(), ntiles);

    // ---- IFD ----
    let ifd_off = f.stream_position()?;
    const N_ENTRIES: u64 = 12;
    // BigTIFF 内联规则：count*类型大小 <= 8 的值直接放条目 value 字段。
    // BitsPerSample(3×SHORT=6B) 因此内联为 0x0008000800080008；
    // TileOffsets/TileByteCounts 数据量远超 8B，必须外置。
    let extras_start = ifd_off + 8 + N_ENTRIES * 20 + 8; // 头 + 12 条目 + next 指针
    let offsets_off = extras_start;
    let counts_off = offsets_off + (ntiles as u64) * 8;

    f.write_all(&N_ENTRIES.to_le_bytes())?;
    // 按 tag 升序写条目（TIFF 规范要求）
    let entry = |f: &mut BufWriter<File>, tag: u16, ty: u16, count: u64, value: u64| {
        let mut e = Vec::with_capacity(20);
        e.extend_from_slice(&tag.to_le_bytes());
        e.extend_from_slice(&ty.to_le_bytes());
        e.extend_from_slice(&count.to_le_bytes());
        e.extend_from_slice(&value.to_le_bytes());
        f.write_all(&e)
    };
    entry(&mut f, TAG_IMAGE_WIDTH, TYPE_LONG, 1, width as u64)?;
    entry(&mut f, TAG_IMAGE_LENGTH, TYPE_LONG, 1, height as u64)?;
    entry(&mut f, TAG_BITS_PER_SAMPLE, TYPE_SHORT, 3, 0x0008000800080008u64)?;
    entry(&mut f, TAG_COMPRESSION, TYPE_SHORT, 1, COMPRESSION_DEFLATE)?;
    entry(&mut f, TAG_PHOTOMETRIC, TYPE_SHORT, 1, PHOTOMETRIC_RGB)?;
    entry(&mut f, TAG_TILE_OFFSETS, TYPE_LONG8, ntiles as u64, offsets_off)?;
    entry(&mut f, TAG_SAMPLES_PER_PIXEL, TYPE_SHORT, 1, 3)?;
    entry(&mut f, TAG_TILE_BYTE_COUNTS, TYPE_LONG8, ntiles as u64, counts_off)?;
    entry(&mut f, TAG_PLANAR_CONFIG, TYPE_SHORT, 1, 1)?;
    entry(&mut f, TAG_TILE_WIDTH, TYPE_SHORT, 1, tile_w as u64)?;
    entry(&mut f, TAG_TILE_LENGTH, TYPE_SHORT, 1, tile_h as u64)?;
    entry(&mut f, TAG_SAMPLE_FORMAT, TYPE_SHORT, 1, 1)?;
    f.write_all(&0u64.to_le_bytes())?; // next IFD = 0

    // ---- 附加数据区：TileOffsets 数组 + TileByteCounts 数组 ----
    for &off in &offsets {
        f.write_all(&off.to_le_bytes())?;
    }
    for &cnt in &counts {
        f.write_all(&cnt.to_le_bytes())?;
    }

    // ---- 回填 IFD 偏移 ----
    f.flush()?;
    let mut file = f.into_inner()?;
    file.seek(SeekFrom::Start(8))?;
    file.write_all(&ifd_off.to_le_bytes())?;
    file.flush()?;
    file.sync_all()?;
    Ok(())
}

/// 用 libdeflater 压缩一段 zlib 流（TIFF deflate 压缩类型 8 要求带 zlib 头，RFC 1950）
fn deflate(data: &[u8], level: u8) -> Vec<u8> {
    let lvl = CompressionLvl::new(level.clamp(1, 12) as i32)
        .unwrap_or_else(|_| CompressionLvl::fastest());
    let mut comp = Compressor::new(lvl);
    let bound = comp.zlib_compress_bound(data.len());
    let mut out = vec![0u8; bound];
    let n = comp
        .zlib_compress(data, &mut out)
        .expect("deflate 压缩失败");
    out.truncate(n);
    out
}
