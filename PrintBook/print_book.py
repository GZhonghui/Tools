# 本代码部分由AI生成

import fitz  # PyMuPDF

def scale_pdf(input_pdf_path, output_pdf_path, scale=74/210):
    # 打开输入的 PDF 文件
    pdf_document = fitz.open(input_pdf_path)
    new_pdf = fitz.open()  # 创建一个新的 PDF 文件

    a3_width = 841.89
    a3_height = 1190.55

    new_page = new_pdf.new_page(width=a3_width, height=a3_height)
    page_idx = 0

    # 遍历每一页，将其缩放到指定比例和位置
    for page_num in range(pdf_document.page_count):
        page = pdf_document[page_num]
        
        # 获取原页面的宽高
        original_width = page.rect.width
        original_height = page.rect.height
        new_width = original_width * scale
        new_height = original_height * scale
        
        # 创建缩放矩阵，并设置偏移
        matrix = fitz.Matrix(scale, scale)

        x_offset = (page_idx % 4) * new_width
        y_offset = (page_idx // 4) * new_height

        # 应用矩阵和偏移，将内容绘制到新页面的中心位置
        new_page.show_pdf_page(
            fitz.Rect(x_offset, y_offset, x_offset + new_width, y_offset + new_height), 
            pdf_document, 
            page_num, 
            clip=page.rect, 
            rotate=0,
            overlay=False,
            keep_proportion=True
        )

        page_idx = page_idx + 1
        if page_idx >= 16:
            page_idx = 0
            new_page = new_pdf.new_page(width=a3_width, height=a3_height)

    # 保存输出 PDF
    new_pdf.save(output_pdf_path)
    new_pdf.close()
    pdf_document.close()

scale_pdf('input_a4.pdf', 'output_a3.pdf')