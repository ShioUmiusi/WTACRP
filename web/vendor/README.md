# vendor/ —— 随页面走的第三方库（不联网、不依赖构建）

## fzstd.min.js

| 项 | 值 |
|---|---|
| 库 | fzstd |
| 版本 | 0.1.1 |
| 许可 | MIT（Copyright (c) 2020 Arjun Barrett）—— 全文见同目录 `fzstd.LICENSE.txt` |
| 来源 | npm 包 `fzstd@0.1.1`，tarball `https://registry.npmjs.org/fzstd/-/fzstd-0.1.1.tgz` 内的 `package/umd/index.js`（minified UMD 构建） |
| 仓库 | https://github.com/101arrowz/fzstd |
| 改动 | **原样 vendor，未做任何修改**；只在文件开头加了一段署名/来源注释（`/*! ... */`） |
| 文件 sha256 | `9fc42499b7afecefaaddfc72846f5e9824c650882471243b9f59633a2ee4a30b`（`fzstd.min.js`，含后加的文件头） |
| 构建体 sha256 | `fbea1c25c4413b620ca3f59e6037d9f6c675c5c7f690e4d660143188f686461d`（原始 `umd/index.js` 的 8394 字节） |

**用法**：普通 `<script src="vendor/fzstd.min.js"></script>`，UMD 会把接口挂到
`window.fzstd = { decompress, Decompress, ZstdErrorCode }`。

**为什么必须自带**：Chromium 的 `DecompressionStream` 只支持 `gzip` / `deflate`，
**不支持 `'zstd'`**（本机 Chromium 实测：`new DecompressionStream('zstd')` 抛
`TypeError`）。而 .wrpl 的包流是单个 zstd 帧，所以解码器必须随页面走。

**实测**：本局 5,116,892 B 的 zstd 帧 → 14,180,794 B 包流，与 Python
`zstandard` 解出的字节**完全相同**（逐字节比对），Chromium 里约 100 ms。

## 其它

本目录没有其它第三方文件。回放格式解析参考
[maxsupermanhd/wrpl-inspector](https://github.com/maxsupermanhd/wrpl-inspector)（AGPL-3.0），
**只用于开发期理解格式**，其代码不进交付产物、也不作为运行时依赖。
