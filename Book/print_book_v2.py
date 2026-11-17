# python 3.11

import fitz # python -m pip install PyMuPDF
import os

def arrange_pages_for_a3(input_pdf_path, output_pdf_path):
    # Open input PDF file
    pdf_document = fitz.open(input_pdf_path)
    total_pages = pdf_document.page_count
    print(f"success: open pdf file, total pages: {total_pages}")
        
    # Create new PDF file
    new_pdf = fitz.open()
        
    # A3 paper size (landscape)
    a3_rect = fitz.paper_rect("a3-l")
    a3_width = a3_rect.width
    a3_height = a3_rect.height
    
    # A4 page size (scaled)
    a4_rect = fitz.paper_rect("a4")
    a4_width = a4_rect.width / 3
    a4_height = a4_rect.height / 3

    print(f"a4_width: {a4_width}, a4_height: {a4_height}")
    print(f"a3_width: {a3_width}, a3_height: {a3_height}")

    def write_row(start_idx: int, y_offset: float, page):
        for _t in range(6):
            i = start_idx + _t
            if i >= total_pages:
                break
            
            page.show_pdf_page(
                fitz.Rect(_t * a4_width, y_offset, _t * a4_width + a4_width, y_offset + a4_height),
                pdf_document,
                i,
                clip=pdf_document[i].rect,
                rotate=0,
                overlay=False,
                keep_proportion=True 
            )
            
            # page number
            page.insert_text(
                (_t * a4_width + 4, y_offset + a4_height - 4),
                str(i+1),
                fontsize=12,
                color=(0, 0, 0)
            )
    
    def draw_grid(page):
        for i in range(1, 6):
            x = i * a4_width
            page.draw_line(
                (x, 0),
                (x, a3_height),
                color=(0, 0, 0),
                width=0.5
            )
            
        for i in range(1, 3):
            y = i * a4_height
            page.draw_line(
                (0, y),
                (a3_width, y), 
                color=(0, 0, 0),
                width=0.5
            )

    def write_page(start_idx: int, k: int):
        page = new_pdf.new_page(width=a3_width, height=a3_height)
        for i in range(k, 6, 2):
            write_row(start_idx +i * 6, (i // 2) * a4_height, page)
        draw_grid(page)

    for i in range(0, total_pages, 36):
        # Create A3 pages (front and back)
        # FUCK YOU PyMuPDF!
        # front_page = new_pdf.new_page(width=a3_width, height=a3_height)
        # print(front_page.parent) # Doc
        # back_page = new_pdf.new_page(width=a3_width, height=a3_height)
        # print(front_page.parent) # None
        
        write_page(i, 0)
        write_page(i, 1)
    
    new_pdf.save(output_pdf_path)
    new_pdf.close()
    pdf_document.close()
    print("processing completed")

if __name__ == "__main__":
    input_pdf = "input_a4.pdf"  # Input A4 PDF file path
    output_pdf = "output_a3.pdf"  # Output A3 PDF file path
    
    # Check if input file exists
    if not os.path.exists(input_pdf):
        print(f"error: input file '{input_pdf}' not found")
        exit(1)
        
    arrange_pages_for_a3(input_pdf, output_pdf)
