//! 瓦片扫描与解码：读取 `{matrix}_{col}_{row}.png` 并解出 RGB8 像素。
//!
//! 与 Python 版行为保持一致：
//! - 源瓦片是调色板 PNG（可能带透明），一律展开为 RGB8 并丢弃 alpha
//!   （等价于 PIL 的 `convert('RGB')`）
//! - 缺失瓦片由调用方填黑（等价于 Python 中 block 初始化的 0）

use std::fs::File;
use std::io::BufReader;
use std::path::{Path, PathBuf};

pub struct TileGrid {
    pub tiles_dir: PathBuf,
    pub matrix: u32,
    pub col_start: u32,
    pub row_start: u32,
    pub tile_w: u32,
    pub tile_h: u32,
    pub cols: usize,
    pub rows: usize,
    pub missing: usize,
}

impl TileGrid {
    /// v2 分级路径：{dir}/{matrix}/{row}/{col}.png
    fn tile_path(&self, tc: u32, tr: u32) -> PathBuf {
        self.tiles_dir
            .join(self.matrix.to_string())
            .join((self.row_start + tr).to_string())
            .join(format!("{}.png", self.col_start + tc))
    }

    /// v1 扁平路径：{dir}/{matrix}_{col}_{row}.png（旧结构兼容）
    fn legacy_tile_path(&self, tc: u32, tr: u32) -> PathBuf {
        self.tiles_dir.join(format!(
            "{}_{}_{}.png",
            self.matrix,
            self.col_start + tc,
            self.row_start + tr
        ))
    }

    /// 查找瓦片：优先 v2，回退 v1；存在返回路径，否则 None
    fn find_tile(&self, tc: u32, tr: u32) -> Option<PathBuf> {
        let p = self.tile_path(tc, tr);
        if p.is_file() {
            return Some(p);
        }
        let lp = self.legacy_tile_path(tc, tr);
        lp.is_file().then_some(lp)
    }

    /// 读取一块瓦片，解码为 RGB8（w*h*3 字节）。
    /// 文件不存在或解码失败返回 None。
    pub fn read_tile(&self, tc: u32, tr: u32) -> Option<Vec<u8>> {
        let path = self.find_tile(tc, tr)?;
        decode_rgb(&path).ok()
    }
}

/// 预扫描：确认目录、探测瓦片尺寸、统计缺失数
pub fn scan(
    tiles_dir: &Path,
    matrix: u32,
    col_start: u32,
    col_end: u32,
    row_start: u32,
    row_end: u32,
) -> Result<TileGrid, String> {
    if !tiles_dir.is_dir() {
        return Err(format!("瓦片目录不存在: {}", tiles_dir.display()));
    }
    let cols = (col_end - col_start + 1) as usize;
    let rows = (row_end - row_start + 1) as usize;

    // 先构造网格（尺寸稍后探测），以便复用 find_tile（v2 优先 / v1 回退）
    let grid = TileGrid {
        tiles_dir: tiles_dir.to_path_buf(),
        matrix,
        col_start,
        row_start,
        tile_w: 0,
        tile_h: 0,
        cols,
        rows,
        missing: 0,
    };

    // 探测尺寸：用范围左上角第一片；缺失则用目录里任一瓦片
    let (tile_w, tile_h) = match grid.find_tile(0, 0) {
        Some(p) => decode_dims(&p).map_err(|e| format!("瓦片解码失败 {}: {}", p.display(), e))?,
        None => {
            // 取目录中任一瓦片（递归寻找）仅用于探测尺寸
            let mut any: Option<PathBuf> = None;
            if let Ok(rd) = std::fs::read_dir(tiles_dir) {
                for e in rd.flatten() {
                    let p = e.path();
                    if p.is_file() && p.extension().is_some_and(|x| x == "png") {
                        any = Some(p);
                        break;
                    }
                }
            }
            match any {
                Some(p) => {
                    decode_dims(&p).map_err(|e| format!("瓦片解码失败 {}: {}", p.display(), e))?
                }
                None => return Err(format!("瓦片目录为空: {}", tiles_dir.display())),
            }
        }
    };

    // 统计缺失（兼容新旧结构）
    let mut missing = 0usize;
    for tr in 0..rows as u32 {
        for tc in 0..cols as u32 {
            if grid.find_tile(tc, tr).is_none() {
                missing += 1;
            }
        }
    }

    Ok(TileGrid {
        tile_w,
        tile_h,
        missing,
        ..grid
    })
}

fn decode_dims(path: &Path) -> Result<(u32, u32), String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let dec = png::Decoder::new(BufReader::new(file));
    let reader = dec.read_info().map_err(|e| e.to_string())?;
    Ok((reader.info().width, reader.info().height))
}

fn decode_rgb(path: &Path) -> Result<Vec<u8>, String> {
    let file = File::open(path).map_err(|e| e.to_string())?;
    let mut dec = png::Decoder::new(BufReader::new(file));
    // EXPAND: 调色板/灰度/低位深 → 8bit RGB(A)；随后按实际色彩模式归一为 RGB
    dec.set_transformations(png::Transformations::EXPAND);
    let mut reader = dec.read_info().map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; reader.output_buffer_size().unwrap_or(0)];
    let info = reader.next_frame(&mut buf).map_err(|e| e.to_string())?;

    let (w, h) = (info.width as usize, info.height as usize);
    match info.color_type {
        png::ColorType::Rgb => {
            buf.truncate(w * h * 3);
            Ok(buf)
        }
        png::ColorType::Rgba => Ok(buf
            .chunks_exact(4)
            .flat_map(|p| [p[0], p[1], p[2]])
            .collect()),
        png::ColorType::Grayscale => Ok(buf
            .iter()
            .take(w * h)
            .flat_map(|&g| [g, g, g])
            .collect()),
        other => Err(format!("不支持的色彩模式: {:?}", other)),
    }
}

/// 当前进程峰值内存（Linux /proc 的 VmHWM，单位 MB）
pub fn peak_rss_mb() -> f64 {
    let s = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    for line in s.lines() {
        if let Some(rest) = line.strip_prefix("VmHWM:") {
            if let Some(kb) = rest.trim().strip_suffix(" kB") {
                if let Ok(k) = kb.trim().parse::<f64>() {
                    return k / 1024.0;
                }
            }
        }
    }
    0.0
}
