"""USB spectrometer control and spectrum display widgets."""

from ._bootstrap import *


class SpectrometerControlWidget(QFrame):
    signal_newExposureTime = Signal(float)
    signal_newAnalogGain = Signal(float)

    def __init__(self, spectrometer, streamHandler, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spectrometer = spectrometer
        self.streamHandler = streamHandler
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.btn_live = QPushButton("Live")
        self.btn_live.setCheckable(True)
        self.btn_live.setChecked(False)
        self.btn_live.setDefault(False)

        self.entry_exposureTime = QDoubleSpinBox()
        self.entry_exposureTime.setMinimum(0.001)
        self.entry_exposureTime.setMaximum(5000)
        self.entry_exposureTime.setSingleStep(1)
        self.entry_exposureTime.setValue(50)
        self.entry_exposureTime.setKeyboardTracking(False)
        self.spectrometer.set_integration_time_ms(50)

        self.btn_live.clicked.connect(self.toggle_live)
        self.entry_exposureTime.valueChanged.connect(self.spectrometer.set_integration_time_ms)

        grid_line2 = QHBoxLayout()
        grid_line2.addWidget(QLabel("USB spectrometer"))
        grid_line2.addWidget(QLabel("Integration Time (ms)"))
        grid_line2.addWidget(self.entry_exposureTime)
        grid_line2.addWidget(self.btn_live)

        self.grid = QVBoxLayout()
        self.grid.addLayout(grid_line2)
        self.setLayout(self.grid)

    def toggle_live(self, pressed):
        if pressed:
            self.spectrometer.start_streaming()
        else:
            self.spectrometer.pause_streaming()


class SpectrumPlotGraphicsWidget(pg.GraphicsLayoutWidget):
    """pyqtgraph plot container for spectrum lines (name avoids clash with tracking_and_controls.PlotWidget)."""

    def __init__(self, title="", parent=None, add_legend=False):
        super().__init__(parent)
        self.plotWidget = self.addPlot(title=title)
        if add_legend:
            self.plotWidget.addLegend()

    def plot(self, x, y, clear=False):
        self.plotWidget.plot(x, y, clear=clear)


class SpectrumDisplay(QFrame):
    def __init__(self, N=1000, main=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.N = N
        self.add_components()
        self.setFrameStyle(QFrame.Panel | QFrame.Raised)

    def add_components(self):
        self.plotWidget = SpectrumPlotGraphicsWidget("", add_legend=True)

        layout = QGridLayout()
        layout.addWidget(self.plotWidget, 0, 0)
        self.setLayout(layout)

    def plot(self, data):
        self.plotWidget.plot(data[0, :], data[1, :], clear=True)
