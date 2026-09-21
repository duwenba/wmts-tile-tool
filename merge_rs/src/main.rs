//! WMTS 瓦片合并工具（Rust 重写）
//!
//! 用法：
//!   merge_rs --matrix 16 --col-start 53248 --col-end 53521 \
//!            --row-start 10558 --row-end 10740 [选项]
//!
//! 输出格式：
//!   tif  （默认）分块 BigTIFF，无损 deflate，逐瓦片并行压缩，内存恒定，适合超大图
//!   png          单张 PNG，流式写出，内存≈一行+一片，适合中等成图
//!
//! 与 Python 版差异：
//!   - 内存峰值与整图大小无关（tif），7.5GB 机器上可合并几十 GB 的超大图
//!   - 8 核并行压缩，速度提升数倍
//!   - 缺失瓦片填黑，行为与 Python 版一致

mod png_out;
mod tiff_out;
mod tile;

use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Instant;

use tile::TileGrid;

struct Args {
    matrix: u32,
    col_start: u32,
    col_end: u32,
    row_start: u32,
    row_end: u32,
    tiles_dir: PathBuf,
    out: PathBuf,
    format: Format,
    threads: Option<usize>,
    level: u8,
    progress_json: bool,
}

#[derive(Clone, Copy, PartialEq)]
enum Format {
    Tif,
    Png,
}

const USAGE: &str = "\
WMTS 瓦片合并工具（Rust，超大图低内存）

用法:
  merge_rs --matrix M --col-start CS --col-end CE --row-start RS --row-end RE [选项]

必选:
  --matrix M          缩放级别
  --col-start CS      列起始
  --col-end CE        列结束
  --row-start RS      行起始
  --row-end RE        行结束

选项:
  --tiles-dir DIR     瓦片目录（默认 tiles）
  --out FILE          输出文件（默认 merged_<m>_<cs>_<ce>_<rs>_<re>.tif）
  --format tif|png    输出格式（默认按扩展名判断，否则 tif）
  --threads N        并行线程数（默认 CPU 核数）
  --level L          压缩级别 0-12（tif 默认 6；png 默认 1，值域 0-9）
  --progress-json    进度以 JSON 行输出到 stderr（供 GUI/脚本解析）
  --help             显示帮助

示例:
  merge_rs --matrix 16 --col-start 53248 --col-end 53521 --row-start 10558 --row-end 10740
  merge_rs --matrix 16 --col-start 53248 --col-end 53257 --row-start 10558 --row-end 10567 --format png --out test.png
";

fn fail(msg: &str) -> ExitCode {
    eprintln!("错误: {}", msg);
    ExitCode::FAILURE
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(Some(a)) => a,
        Ok(None) => return ExitCode::SUCCESS, // --help
        Err(e) => return fail(&e),
    };

    if let Some(n) = args.threads {
        let _ = rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global();
    }

    // 1. 预扫描
    let grid = match tile::scan(
        &args.tiles_dir,
        args.matrix,
        args.col_start,
        args.col_end,
        args.row_start,
        args.row_end,
    ) {
        Ok(g) => g,
        Err(e) => return fail(&e),
    };

    let width = (grid.cols as u32) * grid.tile_w;
    let height = (grid.rows as u32) * grid.tile_h;
    let total = grid.cols * grid.rows;

    println!("瓦片尺寸: {}x{}", grid.tile_w, grid.tile_h);
    println!(
        "拼接尺寸: {}x{} ({}列 x {}行 = {} 瓦片)",
        width, height, grid.cols, grid.rows, total
    );
    if grid.missing > 0 {
        println!("警告: 缺失 {} 瓦片（将填黑）", grid.missing);
    }

    let start = Instant::now();
    let result = match args.format {
        Format::Tif => merge_tif(&grid, width, height, &args.out, args.level, args.progress_json),
        Format::Png => merge_png(&grid, width, height, &args.out, args.level, args.progress_json),
    };

    match result {
        Ok(()) => {
            let size_mb = std::fs::metadata(&args.out)
                .map(|m| m.len() as f64 / (1024.0 * 1024.0))
                .unwrap_or(0.0);
            if args.progress_json {
                eprintln!(
                    "{{\"done\":true,\"output\":\"{}\",\"size_mb\":{:.2},\"elapsed\":{:.1},\"peak_mb\":{:.1}}}",
                    args.out.display(),
                    size_mb,
                    start.elapsed().as_secs_f64(),
                    tile::peak_rss_mb()
                );
            } else {
                println!("\n完成！输出: {} ({:.2} MB)", args.out.display(), size_mb);
                println!("耗时: {:.1} 秒", start.elapsed().as_secs_f64());
                println!("峰值内存: {:.1} MB", tile::peak_rss_mb());
            }
            ExitCode::SUCCESS
        }
        Err(e) => fail(&format!("合并失败: {}", e)),
    }
}

fn merge_tif(
    grid: &TileGrid,
    width: u32,
    height: u32,
    out: &Path,
    level: u8,
    progress_json: bool,
) -> std::io::Result<()> {
    let start = Instant::now();
    let (tw, th) = (grid.tile_w, grid.tile_h);

    // get_tile 需要在并行闭包里被调用：grid 本身是 Sync 的
    let get_tile = |tc: u32, tr: u32| grid.read_tile(tc, tr);

    let progress = move |done: usize, all: usize| {
        if all == 0 {
            return;
        }
        let pct = done as f64 * 100.0 / all as f64;
        let secs = start.elapsed().as_secs_f64();
        let eta = if done > 0 {
            secs / done as f64 * (all - done) as f64
        } else {
            0.0
        };
        if progress_json {
            eprintln!(
                "{{\"percent\":{:.1},\"tiles\":{},\"total\":{},\"elapsed\":{:.0},\"eta\":{:.0}}}",
                pct, done, all, secs, eta
            );
        } else {
            eprint!(
                "\r拼接: {:.1}% ({}/{} 瓦片) | 已用 {:.0}s | 预计剩余 {:.0}s  ",
                pct, done, all, secs, eta
            );
        }
    };

    tiff_out::write_bigtiff(out, width, height, tw, th, level, &progress, get_tile)
}

fn merge_png(
    grid: &TileGrid,
    width: u32,
    height: u32,
    out: &Path,
    level: u8,
    progress_json: bool,
) -> std::io::Result<()> {
    let (tw, th) = (grid.tile_w as usize, grid.tile_h as usize);
    let (cols, rows) = (grid.cols, grid.rows);
    let row_bytes = width as usize * 3;

    let mut pw = png_out::PngWriter::new(out, width, height, level as u32)?;
    let start = Instant::now();

    // 逐瓦片行处理：先拼出一整条瓦片行的像素缓冲（256 行），再逐行写出
    // 峰值内存 = 一条瓦片行缓冲（256*width*3）+ 单片瓦片
    for tr in 0..rows {
        let mut band = vec![0u8; th * row_bytes];
        for tc in 0..cols {
            if let Some(rgb) = grid.read_tile(tc as u32, tr as u32) {
                // rgb 为 tw*th*3；拷贝到 band 对应列
                for j in 0..th {
                    let src = &rgb[j * tw * 3..(j + 1) * tw * 3];
                    let dst = j * row_bytes + tc * tw * 3;
                    band[dst..dst + src.len()].copy_from_slice(src);
                }
            }
        }
        for j in 0..th {
            pw.write_row(&band[j * row_bytes..(j + 1) * row_bytes])?;
        }

        let done = tr + 1;
        if progress_json {
            eprintln!(
                "{{\"percent\":{:.1},\"rows\":{},\"total_rows\":{}}}",
                done as f64 * 100.0 / rows as f64,
                done,
                rows
            );
        } else {
            eprint!(
                "\r拼接: {:.1}% ({}/{} 行) | 已用 {:.0}s  ",
                done as f64 * 100.0 / rows as f64,
                done,
                rows,
                start.elapsed().as_secs_f64()
            );
        }
    }
    pw.finish()
}

// ---------------- 参数解析 ----------------

fn parse_args() -> Result<Option<Args>, String> {
    let argv: Vec<String> = env::args().skip(1).collect();
    if argv.is_empty() || argv.iter().any(|a| a == "--help" || a == "-h") {
        print!("{}", USAGE);
        return Ok(None);
    }

    let mut m = std::collections::HashMap::<String, String>::new();
    let mut flags = std::collections::HashSet::<String>::new();
    let mut it = argv.iter();
    while let Some(k) = it.next() {
        if k == "--progress-json" {
            flags.insert(k.clone());
            continue;
        }
        let v = it.next().ok_or_else(|| format!("参数 {} 缺少值", k))?;
        m.insert(k.clone(), v.clone());
    }

    let get = |k: &str| -> Result<String, String> {
        m.get(k)
            .cloned()
            .ok_or_else(|| format!("缺少必选参数 --{}", k))
    };
    let get_u = |k: &str| -> Result<u32, String> {
        get(k)?
            .parse()
            .map_err(|_| format!("参数 --{} 需要整数", k))
    };

    let matrix = get_u("--matrix")?;
    let col_start = get_u("--col-start")?;
    let col_end = get_u("--col-end")?;
    let row_start = get_u("--row-start")?;
    let row_end = get_u("--row-end")?;
    if col_end < col_start || row_end < row_start {
        return Err("范围无效：--col-end/--row-end 必须 >= 起始值".into());
    }

    let tiles_dir = m
        .get("--tiles-dir")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("tiles"));

    let fmt_arg = m.get("--format").cloned();
    let default_out = PathBuf::from(format!(
        "merged_{}_{}_{}_{}_{}.tif",
        matrix, col_start, col_end, row_start, row_end
    ));
    let out = m.get("--out").map(PathBuf::from).unwrap_or(default_out);

    // 格式：显式 --format 优先，否则按扩展名，最后默认 tif
    let format = match fmt_arg.as_deref() {
        Some("tif") | Some("tiff") => Format::Tif,
        Some("png") => Format::Png,
        Some(other) => return Err(format!("未知格式 '{}'（支持 tif / png）", other)),
        None => match out.extension().and_then(|e| e.to_str()) {
            Some("png") => Format::Png,
            _ => Format::Tif,
        },
    };

    let threads = m
        .get("--threads")
        .map(|s| {
            s.parse::<usize>()
                .map_err(|_| "--threads 需要整数".to_string())
        })
        .transpose()?;

    let level = match m.get("--level") {
        Some(s) => s
            .parse::<u8>()
            .map_err(|_| "--level 需要整数".to_string())?,
        None => match format {
            Format::Tif => 6,
            Format::Png => 1,
        },
    };

    Ok(Some(Args {
        matrix,
        col_start,
        col_end,
        row_start,
        row_end,
        tiles_dir,
        out,
        format,
        threads,
        level,
        progress_json: flags.contains("--progress-json"),
    }))
}
