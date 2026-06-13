#!/usr/bin/env python3
import tkinter as tk
import sys

class TouchCalibrator(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Touch Calibration Tool")
        
        # Make it full screen
        self.attributes('-fullscreen', True)
        self.configure(bg='black')
        self.config(cursor="none") # Hide mouse cursor so we see where touch actually lands

        self.width = self.winfo_screenwidth()
        self.height = self.winfo_screenheight()
        
        print(f"Detected screen resolution: {self.width}x{self.height}")

        self.points = [
            (50, 50),                                      # Top Left
            (self.width - 50, 50),                         # Top Right
            (50, self.height - 50),                        # Bottom Left
            (self.width - 50, self.height - 50)            # Bottom Right
        ]
        
        self.point_names = ["Top-Left", "Top-Right", "Bottom-Left", "Bottom-Right"]
        self.current_point = 0
        self.results = []

        self.canvas = tk.Canvas(self, width=self.width, height=self.height, bg='black', highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Bind touch/click event
        self.bind("<Button-1>", self.on_touch)
        
        # Allow exiting with Escape
        self.bind("<Escape>", lambda e: self.destroy())

        self.draw_next_point()

    def draw_next_point(self):
        self.canvas.delete("all")
        
        if self.current_point < len(self.points):
            x, y = self.points[self.current_point]
            name = self.point_names[self.current_point]
            
            # Draw crosshair
            size = 30
            self.canvas.create_line(x - size, y, x + size, y, fill="red", width=3)
            self.canvas.create_line(x, y - size, x, y + size, fill="red", width=3)
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="white")
            
            # Draw instructions
            msg = f"Touch the RED crosshair exactly in the center.\nPoint {self.current_point + 1}/4: {name}\n\n(Press ESC to abort)"
            self.canvas.create_text(self.width // 2, self.height // 2, text=msg, fill="white", font=("Arial", 24), justify="center")
        else:
            self.finish_calibration()

    def on_touch(self, event):
        if self.current_point >= len(self.points): return
        
        # Record the raw coordinate reported by the OS
        touch_x = event.x_root
        touch_y = event.y_root
        
        expected_x, expected_y = self.points[self.current_point]
        name = self.point_names[self.current_point]
        
        self.results.append({
            "name": name,
            "expected_x": expected_x,
            "expected_y": expected_y,
            "actual_x": touch_x,
            "actual_y": touch_y
        })
        
        print(f"Point {self.current_point + 1} ({name}): Expected ({expected_x}, {expected_y}), Got ({touch_x}, {touch_y})")
        
        self.current_point += 1
        self.draw_next_point()

    def finish_calibration(self):
        self.canvas.delete("all")
        self.canvas.create_text(self.width // 2, self.height // 2, text="Calibration complete!\nProcessing data...", fill="green", font=("Arial", 32))
        self.update()
        
        print("\n--- CALIBRATION RESULTS ---")
        for r in self.results:
            dx = r['actual_x'] - r['expected_x']
            dy = r['actual_y'] - r['expected_y']
            print(f"{r['name']:<15} Expected: ({r['expected_x']:>4}, {r['expected_y']:>4})  Actual: ({r['actual_x']:>4}, {r['actual_y']:>4})  Diff: dx={dx:>4}, dy={dy:>4}")
        print("---------------------------\n")
        print("Please copy and paste these results to the AI.")
        
        self.after(3000, self.destroy)

if __name__ == "__main__":
    # Ensure no matrix is currently messing with the input
    print("Please make sure the screen is in 'Normal' rotation before running this.")
    app = TouchCalibrator()
    app.mainloop()