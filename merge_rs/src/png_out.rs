//! 流式单张 PNG 写入器（无损，Sub 滤波 + zlib）。
//!
//! 内存模型：一次只缓冲一行像素 + 当前批次的瓦片；不持有整图。
//! 压缩用 flate2（miniz_oxide，纯 Rust），适合中等成图；
//! 超大图请用 TIFF 分块模式（见 tiff_out.rs，可并行且内存更省）。

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::Path;

use flate2::write::ZlibEncoder;
use flate2::Compression;

const IDAT_CHUNK: usize = 128 * 1024;

/// 把压缩输出切成固定大小的 IDAT 块，防止单块过大
struct ChunkSink<W: Write> {
    inner: W,
    buf: Vec<u8>,
}

impl<W: Write> Write for ChunkSink<W> {
    fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
        self.buf.extend_from_slice(data);
        if self.buf.len() >= IDAT_CHUNK {
            self.flush_chunk()?;
        }
        Ok(data.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<W: Write> ChunkSink<W> {
    fn new(inner: W) -> Self {
        ChunkSink {
            inner,
            buf: Vec::with_capacity(IDAT_CHUNK),
        }
    }
    fn flush_chunk(&mut self) -> std::io::Result<()> {
        if !self.buf.is_empty() {
            write_chunk(&mut self.inner, b"IDAT", &self.buf)?;
            self.buf.clear();
        }
        Ok(())
    }
    fn finish(mut self) -> std::io::Result<W> {
        self.flush_chunk()?;
        Ok(self.inner)
    }
}

fn write_chunk<W: Write>(f: &mut W, ctype: &[u8; 4], data: &[u8]) -> std::io::Result<()> {
    f.write_all(&(data.len() as u32).to_be_bytes())?;
    f.write_all(ctype)?;
    f.write_all(data)?;
    // PNG 规范：CRC 覆盖 chunk 类型 + 数据
    let table = crc_table();
    let mut crc = crc32_update(0xFFFFFFFF, ctype, table);
    crc = crc32_update(crc, data, table) ^ 0xFFFFFFFF;
    f.write_all(&crc.to_be_bytes())?;
    Ok(())
}

/// 极简 CRC32（PNG 需要，自己实现避免额外依赖）
fn crc_table() -> &'static [u32; 256] {
    use std::sync::OnceLock;
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for (i, e) in t.iter_mut().enumerate() {
            let mut c = i as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 { 0xEDB88320 ^ (c >> 1) } else { c >> 1 };
            }
            *e = c;
        }
        t
    })
}

fn crc32_update(mut crc: u32, data: &[u8], table: &[u32; 256]) -> u32 {
    for &b in data {
        crc = table[((crc ^ b as u32) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc
}

pub struct PngWriter {
    width: u32,
    zlib: ZlibEncoder<ChunkSink<BufWriter<File>>>,
}

impl PngWriter {
    /// 创建 PNG（RGB8，无隔行），level=0..9
    pub fn new(path: &Path, width: u32, height: u32, level: u32) -> std::io::Result<Self> {
        // 签名 + IHDR 直接写盘，之后 zlib 压缩输出经 ChunkSink 切成 IDAT 块
        let mut f = BufWriter::with_capacity(1 << 16, File::create(path)?);
        f.write_all(b"\x89PNG\r\n\x1a\n")?;
        let mut ihdr = Vec::with_capacity(13);
        ihdr.extend_from_slice(&width.to_be_bytes());
        ihdr.extend_from_slice(&height.to_be_bytes());
        ihdr.extend_from_slice(&[8, 2, 0, 0, 0]); // 8bit, color=2(RGB), 无压缩预置/滤波/隔行
        write_chunk(&mut f, b"IHDR", &ihdr)?;
        let sink = ChunkSink::new(f);
        let zlib = ZlibEncoder::new(sink, Compression::new(level));
        Ok(PngWriter { width, zlib })
    }

    /// 写入一行像素（必须恰好 width*3 字节）
    pub fn write_row(&mut self, row: &[u8]) -> std::io::Result<()> {
        debug_assert_eq!(row.len() as u32, self.width * 3);
        // PNG Sub 滤波（bpp=3）：out[i] = row[i] - row[i-3] (mod 256)
        let mut filtered = Vec::with_capacity(row.len() + 1);
        filtered.push(0x01); // filter type 1 (Sub)
        for i in 0..row.len() {
            let left = if i >= 3 { row[i - 3] } else { 0 };
            filtered.push(row[i].wrapping_sub(left));
        }
        self.zlib.write_all(&filtered)
    }

    /// 结束并关闭
    pub fn finish(self) -> std::io::Result<()> {
        let sink = self.zlib.finish()?; // 吐出剩余压缩数据到 sink
        let mut f = sink.finish()?; // 落 IDAT 剩余块
        write_chunk(&mut f, b"IEND", &[])?;
        f.flush()?;
        Ok(())
    }
}
