# Ebook2XTX
项目更名为Ebook2XTX，原ZipComic2XTC项目已删除，如需请看ZipComic2XTC分支，新版本将在ZipComic2XTC原有基础上新增功能。

软件需要安装Python运行环境，会自动安装剩余所需的运行组件，需下载UnRAR.exe（跟py文件放一起）才能运行 或者使用命令打包成单文件：

控制台版本：python -m PyInstaller --onefile --console --name Ebook2XTX --add-data "UnRAR.exe;." --collect-all numba --collect-all llvmlite --hidden-import numpy --hidden-import py7zr --hidden-import rarfile ebook2xtx.py

GUI：python -m PyInstaller --onefile --noconsole --name Ebook2XTX_GUI --add-data "UnRAR.exe;." --collect-all numba --collect-all llvmlite --hidden-import numpy --hidden-import py7zr --hidden-import rarfile ebook2xtx_GUI.py

支持输入的格式：XTC (1-bit 黑白，容器格式) XTCH (2-bit 4级灰度，容器格式) XTG (1-bit 黑白，单页模式) XTH (2-bit 4级灰度，单页模式)，压缩包（zip，rar，7z，cbr，cbz等）格式的图片（jpg，bmp，png，webp等）漫画，文件夹形式的图片漫画（jpg，bmp，png，webp等），电子书格式（pdf，epub，mobi，azw3）的漫画，纯文本/图文混排的电子书格式暂不支持（仅支持导出图片或导出文本为txt文件）。
支持转换的格式： XTC (1-bit 黑白，容器格式，单文件) XTCH (2-bit 4级灰度，容器格式，单文件) XTG (1-bit 黑白，单页模式，每个图片单独输出) XTH (2-bit 4级灰度，单页模式，每个图片单独输出)，图片格式（jpg，png，webp，bmp），电子书格式（epub，pdf）

可选择转换分辨率： X4（480×800）（默认），X4双倍分辨率（960×1600），X3 （528×792），X3双倍分辨率（1056×1584），原图分辨率，自定义分辨率
漫画白边自动裁切： 开（默认）/关
画面切割： 
横切2图：1:1.618，1.618:1，1:1（其中1:1模式下支持切2图+1中间帧）
横切3图：1:2:1，2:1:1，1:1:2，1:1:1（其中1:1:1模式下支持切3图+2中间帧）
漫画内横版图片自动旋转： 不旋转 顺时针旋转90度（默认，x4按键在右面） 逆时针90度
拉伸至全屏： 开（默认）/关
抖动强度： 默认0.7（可调范围0-1）
容器格式分包： 4G（FAT32），自定义大小或不分包
同时处理的线程数量： 默认为CPU线程数，不建议修改，经测试能跑满CPU
GIF文件处理：转换第一帧（默认），转换所有帧
透明支持：不可修改，默认将透明部分处理成白色（255,255,255），因为大部分墨水屏设备底色为白色
输出非容器格式文件名命名格式：编号.格式（默认），书名-编号.格式
电子书格式支持自动判断类型：图片（完整支持），文本（支持单独导出为txt），图文混排（仅支持单独导出为txt或单独导出图片，建议先转换成pdf格式再使用本软件）

实现原理参考了bigbag大佬的epub-to-xtc-converter给大佬点赞：https://github.com/bigbag/epub-to-xtc-converter
