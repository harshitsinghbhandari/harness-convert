# frozen_string_literal: true

# Formula for harness-convert.
class HarnessConvert < Formula
  include Language::Python::Virtualenv

  desc "Relocate a coding-agent session across harnesses and resume it natively"
  homepage "https://hc.agentlab.in"
  url "https://github.com/harshitsinghbhandari/harness-convert/releases/download/v0.2.0/harness_convert-0.2.0.tar.gz"
  sha256 "f97b15565585cc786bbfae7419652cf61f0fd6e5d561a112648d521f1e175967"
  license "MIT"

  depends_on "python@3.14"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "relocate a coding-agent session", shell_output("#{bin}/hc --help")
  end
end
